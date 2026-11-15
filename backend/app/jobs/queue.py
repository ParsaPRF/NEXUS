"""Job queue abstraction and the in-process implementation.

The abstraction
---------------
:class:`JobQueue` is the seam between "the application wants work done later" and "some
mechanism runs it". Phase 1 provides :class:`InProcessJobQueue`; Phase 5 adds an RQ/Redis
implementation. Callers depend on the protocol, so the swap is a configuration change.

What this module deliberately does **not** do yet
-------------------------------------------------
It does not persist job state. Persistence belongs to ``background_jobs`` in the database and
arrives in Phase 5 together with the worker that owns those rows. Duplicating a half-finished
job store here would create two sources of truth, which is exactly the kind of shortcut that
costs a week later.

What it does provide
--------------------
* A **cooperative cancellation** flag. Jobs check :meth:`JobContext.check_cancelled` at safe
  points. Nothing is killed mid-transaction, because interrupting a thread in the middle of a
  database write leads to corruption, not speed.
* **Progress reporting**, because the Task Monitor needs a numerator and a denominator.
* A **handler registry**, so a caller enqueues by name (which is what a Redis-backed queue
  needs anyway - a job name and a JSON payload, not a Python closure).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.exceptions import JobCancelledError, NotFoundError, ValidationError
from app.core.logging import get_logger, redact

logger = get_logger(__name__)

#: A job handler receives a context and the (already redacted) payload, and returns a result.
JobHandler = Callable[["JobContext", dict[str, Any]], Awaitable[dict[str, Any] | None]]

#: Registry of job type name -> handler. Populated by modules at import time.
_HANDLERS: dict[str, JobHandler] = {}


def register_handler(job_type: str, handler: JobHandler) -> None:
    """Register the callable that executes ``job_type``.

    Raises:
        ValidationError: the name is empty or already taken by a different handler. Rejecting a
            silent overwrite prevents two modules from fighting over one job type.
    """
    if not job_type:
        raise ValidationError("A job type name is required.")
    existing = _HANDLERS.get(job_type)
    if existing is not None and existing is not handler:
        raise ValidationError(
            f"Job type {job_type!r} is already registered to a different handler.",
            details={"job_type": job_type},
        )
    _HANDLERS[job_type] = handler


def get_handler(job_type: str) -> JobHandler:
    """Return the handler for ``job_type``.

    Raises:
        NotFoundError: no handler is registered. Enqueuing an unregistered job is refused rather
            than accepted and then lost inside a worker.
    """
    try:
        return _HANDLERS[job_type]
    except KeyError as exc:
        raise NotFoundError(
            f"No handler is registered for job type {job_type!r}.",
            details={"job_type": job_type, "known": sorted(_HANDLERS)},
        ) from exc


def registered_job_types() -> list[str]:
    """Return the registered job type names, sorted."""
    return sorted(_HANDLERS)


@dataclass(frozen=True, slots=True)
class JobStatusSnapshot:
    """A read-only view of a job's progress, for the Task Monitor UI."""

    job_id: str
    job_type: str
    status: str
    progress_current: int
    progress_total: int
    message: str | None = None

    @property
    def progress_percent(self) -> float:
        """Progress in ``0..100``, or ``0.0`` when the total is unknown."""
        if self.progress_total <= 0:
            return 0.0
        return min(100.0, round(self.progress_current / self.progress_total * 100, 2))


@dataclass
class JobContext:
    """Handed to a running job: identity, progress reporting and cancellation checks.

    A job is expected to call :meth:`set_progress` as it advances and
    :meth:`check_cancelled` between units of work. Both are cheap.

    ``settings`` is the configuration the queue was created with, so a job reads the same
    configuration as the application that enqueued it. Falling back to the process-wide singleton
    would make a test-built application run its jobs against production defaults.
    """

    job_id: str
    job_type: str
    payload: dict[str, Any]
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    progress_current: int = 0
    progress_total: int = 0
    messages: list[str] = field(default_factory=list)
    settings: Any = None

    @property
    def cancelled(self) -> bool:
        """True when cancellation has been requested."""
        return self.cancel_event.is_set()

    def set_total(self, total: int) -> None:
        """Declare how many units of work the job expects to perform."""
        self.progress_total = max(0, total)

    def set_progress(
        self, current: int, *, total: int | None = None, message: str | None = None
    ) -> None:
        """Report progress. ``current`` is clamped to ``0..total``."""
        if total is not None:
            self.progress_total = max(0, total)
        self.progress_current = max(0, min(current, self.progress_total or current))
        if message:
            # A progress message can quote user data, so it goes through redaction.
            self.messages.append(redact(message))

    def advance(self, *, message: str | None = None) -> None:
        """Increment the completed count by one."""
        self.set_progress(self.progress_current + 1, message=message)

    def request_cancel(self) -> None:
        """Ask the job to stop at its next safe point."""
        self.cancel_event.set()

    def check_cancelled(self) -> None:
        """Raise if cancellation was requested.

        Raises:
            JobCancelledError: cancellation is pending. Raising - rather than returning a flag -
                means a job that forgets to check cannot silently ignore a cancel request once it
                does check.
        """
        if self.cancelled:
            raise JobCancelledError(
                f"Job {self.job_id} was cancelled.", details={"job_type": self.job_type}
            )

    def log(self, message: str) -> None:
        """Record a human-readable progress note (redacted)."""
        self.messages.append(redact(message))
        logger.info(
            "[%s] %s", self.job_id, message, extra={"module_name": "jobs", "job_id": self.job_id}
        )


@runtime_checkable
class JobQueue(Protocol):
    """The contract every job backend implements."""

    async def enqueue(
        self, job_type: str, payload: dict[str, Any] | None = None, **options: Any
    ) -> str:
        """Schedule a job and return its identifier."""
        ...

    async def get_status(self, job_id: str) -> JobStatusSnapshot:
        """Return a snapshot of a known job's state."""
        ...

    async def cancel(self, job_id: str) -> bool:
        """Request cancellation. Returns ``False`` when the job is unknown or already finished."""
        ...

    async def shutdown(self) -> None:
        """Stop accepting work and release resources."""
        ...


class InProcessJobQueue:
    """Runs jobs as asyncio tasks inside the API process.

    Suitable for the local profile and for tests. Its honest limitation: work is lost if the
    process dies, and it shares the event loop with request handling, so a heavy job competes
    with the API. Both are exactly why the docker profile uses a real worker (Phase 5).
    """

    def __init__(self, *, max_concurrent_jobs: int = 2, settings: Any = None) -> None:
        if max_concurrent_jobs < 1:
            raise ValidationError("max_concurrent_jobs must be at least 1.")
        self._max_concurrent = max_concurrent_jobs
        self._settings = settings
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._contexts: dict[str, JobContext] = {}
        self._statuses: dict[str, str] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._closed = False

    async def enqueue(
        self, job_type: str, payload: dict[str, Any] | None = None, **options: Any
    ) -> str:
        """Validate, register and start a job.

        The handler is resolved *before* the job is accepted, so an unknown job type fails at
        enqueue time where the caller can see it, instead of inside a background task.

        Raises:
            ValidationError: the queue is shut down.
            NotFoundError: no handler is registered for ``job_type``.
        """
        if self._closed:
            raise ValidationError("The job queue is shut down and cannot accept new jobs.")
        handler = get_handler(job_type)

        job_id = uuid.uuid4().hex
        # Redact the payload before it is held: a job payload can carry an API key or a password.
        safe_payload = redact(payload or {})
        context = JobContext(
            job_id=job_id,
            job_type=job_type,
            payload=safe_payload,
            progress_total=int(options.get("progress_total", 0) or 0),
            settings=self._settings,
        )
        self._contexts[job_id] = context
        self._statuses[job_id] = "queued"
        self._tasks[job_id] = asyncio.create_task(
            self._run(job_id, handler, context), name=f"nexus-job-{job_type}-{job_id}"
        )
        logger.info(
            "Enqueued job %s (%s)",
            job_id,
            job_type,
            extra={"module_name": "jobs", "job_id": job_id},
        )
        return job_id

    async def _run(self, job_id: str, handler: JobHandler, context: JobContext) -> None:
        """Execute one job under the concurrency semaphore."""
        async with self._semaphore:
            if context.cancelled:
                self._statuses[job_id] = "cancelled"
                return
            self._statuses[job_id] = "running"
            try:
                result = await handler(context, context.payload)
                self._statuses[job_id] = "completed"
                if result:
                    context.messages.append(f"result: {redact(result)}")
            except JobCancelledError:
                self._statuses[job_id] = "cancelled"
                logger.info(
                    "Job %s cancelled cooperatively",
                    job_id,
                    extra={"module_name": "jobs", "job_id": job_id},
                )
            except asyncio.CancelledError:
                # The task itself was cancelled (shutdown). Propagate, nothing to log.
                self._statuses[job_id] = "cancelled"
                raise
            except Exception as exc:
                self._statuses[job_id] = "failed"
                # Full detail to the log; the payload is already redacted.
                logger.exception(
                    "Job %s (%s) failed: %s",
                    job_id,
                    context.job_type,
                    type(exc).__name__,
                    extra={"module_name": "jobs", "job_id": job_id},
                )
                context.messages.append(f"failed: {type(exc).__name__}")

    async def get_status(self, job_id: str) -> JobStatusSnapshot:
        """Return the current snapshot for ``job_id``.

        Raises:
            NotFoundError: the id is not known to this queue.
        """
        context = self._contexts.get(job_id)
        if context is None:
            raise NotFoundError("No such job in this queue.", details={"job_id": job_id})
        return JobStatusSnapshot(
            job_id=job_id,
            job_type=context.job_type,
            status=self._statuses.get(job_id, "unknown"),
            progress_current=context.progress_current,
            progress_total=context.progress_total,
            message=context.messages[-1] if context.messages else None,
        )

    async def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation. Returns ``False`` for unknown or finished jobs."""
        context = self._contexts.get(job_id)
        if context is None:
            return False
        if self._statuses.get(job_id) in {"completed", "failed", "cancelled"}:
            return False
        context.request_cancel()
        task = self._tasks.get(job_id)
        # A job that never reaches a check point can hang shutdown; give it a moment, then
        # cancel the task outright. Cooperative first, forceful only as a fallback.
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "Job %s did not stop cooperatively; cancelling the task.",
                    job_id,
                    extra={"module_name": "jobs", "job_id": job_id},
                )
                task.cancel()
                # Await the task so the coroutine's own handler runs and records the final
                # status *before* cancel() returns. Without this the caller can observe a stale
                # "running" state and think cancellation failed.
                with contextlib.suppress(BaseException):
                    await task
            except (JobCancelledError, asyncio.CancelledError):
                pass
            except Exception:  # pragma: no cover - already logged by _run
                pass
        return True

    async def shutdown(self) -> None:
        """Request cancellation of every running job and wait briefly for them."""
        self._closed = True
        for job_id, task in list(self._tasks.items()):
            context = self._contexts.get(job_id)
            if context is not None:
                context.request_cancel()
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    @property
    def active_job_ids(self) -> list[str]:
        """Ids of jobs that have not reached a terminal state."""
        return [
            job_id for job_id, status in self._statuses.items() if status in {"queued", "running"}
        ]


#: Process-wide queue. ``get_queue`` creates it lazily so a test can build an app with its own.
_queue: JobQueue | None = None


def get_queue(settings: Settings | None = None) -> JobQueue:
    """Return the process-wide queue, creating it on first use.

    The backend is chosen by ``NEXUS_JOBS__BACKEND``. Phase 5 adds the ``rq`` branch; until
    then, requesting it raises a clear error instead of silently falling back to the in-process
    runner, because a silent fallback would look like a working Redis setup that is not one.
    """
    global _queue  # noqa: PLW0603 - module-level singleton, same pattern as the DB engine
    if _queue is not None:
        return _queue

    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()

    if settings.jobs.backend == "rq":
        # Imported lazily so the local profile never needs the optional redis/rq packages.
        from app.jobs.rq_queue import RqJobQueue

        _queue = RqJobQueue(settings)
        return _queue

    _queue = InProcessJobQueue(max_concurrent_jobs=settings.jobs.max_workers, settings=settings)
    return _queue


async def reset_queue() -> None:
    """Shut the queue down and forget it. Used at application shutdown and between tests."""
    global _queue  # noqa: PLW0603 - module-level singleton
    if _queue is not None:
        await _queue.shutdown()
    _queue = None


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)

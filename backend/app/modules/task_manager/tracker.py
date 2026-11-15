"""Durable job tracking: keep the database row in step with the running job.

The queue knows how to schedule work; it does not know how to describe it to a user. This module
bridges the two, and deliberately does so through **one wrapper** that every job type is registered
with. That way progress, logs, duration and terminal state are recorded identically for proxy
batches, mail sweeps and campaigns - a job type cannot "forget" to be tracked.

Three behaviours worth knowing:

* **Progress is flushed periodically**, not on every increment. A job that checks 500 proxies would
  otherwise issue 500 UPDATEs; the monitor task samples the in-memory counters instead.
* **Cancellation is read from the database**, so it works for any queue backend. The monitor sees
  ``cancel_requested`` and asks the job context to stop cooperatively at its next safe point;
  nothing is killed mid-transaction.
* **All session access is serialized by a lock.** The monitor and the finishing code share one
  ``AsyncSession``; an ``AsyncSession`` is not concurrency-safe, and using it from two coroutines
  produced a "this transaction is closed" failure before the lock was added.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger, redact
from app.database.engine import get_session_factory
from app.jobs.queue import JobCancelledError, JobContext, register_handler
from app.models.enums import JobLogLevel, JobStatus
from app.models.job_models import BackgroundJob, JobLog

logger = get_logger(__name__)

#: How often the monitor flushes progress and checks for cancellation.
FLUSH_INTERVAL_SECONDS = 0.2

#: Maximum characters kept per log line.
MAX_LOG_MESSAGE = 2000


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    SQLite has no timezone type: a column declared ``DateTime(timezone=True)`` round-trips as a
    *naive* datetime, while PostgreSQL returns an aware one. Comparing the two raises
    ``TypeError``, so every timestamp read back from the database goes through this normalisation.
    Without it, duration tracking silently failed on the local profile.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class JobTracker:
    """Updates one ``background_jobs`` row on behalf of a running job."""

    def __init__(self, session: Any, job_db_id: int) -> None:
        self._session = session
        self._job_db_id = job_db_id
        self._logged_messages = 0
        self._lock = asyncio.Lock()

    async def _job(self) -> BackgroundJob:
        job = await self._session.get(BackgroundJob, self._job_db_id)
        if job is None:
            raise NotFoundError(
                f"Job row {self._job_db_id} was not found.", details={"job_id": self._job_db_id}
            )
        return job

    async def mark_running(self, queue_job_id: str | None) -> None:
        """Record that execution started."""
        async with self._lock:
            job = await self._job()
            job.status = JobStatus.RUNNING.value
            job.started_at = datetime.now(UTC)
            job.attempts = (job.attempts or 0) + 1
            if queue_job_id:
                job.queue_job_id = queue_job_id
            await self._session.commit()

    async def flush_progress(self, current: int, total: int) -> None:
        """Persist progress counters (called periodically by the monitor)."""
        async with self._lock:
            try:
                job = await self._job()
                if job.finished_at is not None:
                    return
                job.progress_current = max(0, current)
                job.progress_total = max(0, total)
                await self._session.commit()
            except Exception:  # noqa: BLE001 - a progress write must never break the job
                await self._session.rollback()
                logger.debug("Could not flush job progress", extra={"module_name": "task_manager"})

    async def _append_messages_locked(self, messages: list[str]) -> None:
        """Add log rows for messages not yet recorded. Caller must hold the lock."""
        pending = messages[self._logged_messages :]
        if not pending:
            return
        self._logged_messages = len(messages)
        for message in pending:
            self._session.add(
                JobLog(
                    job_id=self._job_db_id,
                    level=JobLogLevel.INFO.value,
                    message=redact(message)[:MAX_LOG_MESSAGE],
                )
            )

    async def drain_messages(self, messages: list[str]) -> None:
        """Append any new human-readable messages from the job context as log rows."""
        if len(messages) <= self._logged_messages:
            return
        async with self._lock:
            try:
                await self._append_messages_locked(messages)
                await self._session.commit()
            except Exception:  # noqa: BLE001
                await self._session.rollback()

    async def cancel_requested(self) -> bool:
        """True when an operator asked for cancellation.

        Uses ``populate_existing`` so the value is re-read from the database rather than taken
        from the session's identity map, which may hold the row as it was at start-up.
        """
        async with self._lock:
            try:
                result = await self._session.execute(
                    select(BackgroundJob.cancel_requested)
                    .where(BackgroundJob.id == self._job_db_id)
                    .execution_options(populate_existing=True)
                )
                return bool(result.scalar_one_or_none())
            except Exception:  # noqa: BLE001
                await self._session.rollback()
                return False

    async def finish(
        self,
        *,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        messages: list[str] | None = None,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        """Write the terminal state, duration and any final log lines.

        ``current``/``total`` are the job context's final counters. A short job can finish before
        the monitor's first flush, so the row would otherwise end at 0/0 even though work was
        done - which is exactly what the progress test caught.
        """
        async with self._lock:
            try:
                job = await self._job()
                now = datetime.now(UTC)
                job.status = status.value
                job.finished_at = now
                if total is not None:
                    job.progress_total = max(0, total)
                if current is not None:
                    job.progress_current = max(0, current)
                if job.started_at is not None:
                    job.duration_ms = int((now - _as_utc(job.started_at)).total_seconds() * 1000)
                if result is not None:
                    job.result_json = redact(result)
                if error is not None:
                    job.error_message = redact(error)[:1024]
                if status is JobStatus.COMPLETED:
                    job.progress_current = job.progress_total or job.progress_current
                if messages:
                    await self._append_messages_locked(messages)
                await self._session.commit()
            except Exception:  # noqa: BLE001
                await self._session.rollback()
                logger.exception("Could not record the job's final state")


async def _monitor(tracker: JobTracker, context: JobContext, stop: asyncio.Event) -> None:
    """Flush progress and honour cancellation until the job finishes."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=FLUSH_INTERVAL_SECONDS)
            return
        except TimeoutError:
            pass
        await tracker.flush_progress(context.progress_current, context.progress_total)
        await tracker.drain_messages(context.messages)
        if context.cancelled:
            continue
        if await tracker.cancel_requested():
            context.request_cancel()


def register_tracked_handler(job_type: str, handler: Any) -> None:
    """Register a job handler that keeps its database row up to date.

    Jobs enqueued without ``job_db_id`` (for example, a direct call in a test) run untracked: the
    wrapper degrades to the plain handler rather than inventing a row.
    """

    async def tracked(context: JobContext, payload: dict[str, Any]) -> dict[str, Any] | None:
        job_db_id = payload.get("job_db_id")
        if not job_db_id:
            return await handler(context, payload)

        factory = get_session_factory()
        async with factory() as session:
            tracker = JobTracker(session, int(job_db_id))
            await tracker.mark_running(context.job_id)

            stop = asyncio.Event()
            monitor_task = asyncio.create_task(_monitor(tracker, context, stop))

            async def stop_monitor() -> None:
                """Stop sampling before the final write, so it cannot race it."""
                stop.set()
                with contextlib.suppress(BaseException):
                    await monitor_task

            try:
                result = await handler(context, payload)
            except JobCancelledError:
                await stop_monitor()
                await tracker.finish(
                    status=JobStatus.CANCELLED,
                    messages=context.messages,
                    current=context.progress_current,
                    total=context.progress_total,
                )
                raise
            except Exception as exc:
                await stop_monitor()
                await tracker.finish(
                    status=JobStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    messages=context.messages,
                    current=context.progress_current,
                    total=context.progress_total,
                )
                raise
            else:
                await stop_monitor()
                await tracker.finish(
                    status=JobStatus.COMPLETED,
                    result=result if isinstance(result, dict) else None,
                    messages=context.messages,
                    current=context.progress_current,
                    total=context.progress_total,
                )
                return result

    register_handler(job_type, tracked)


__all__ = [
    "FLUSH_INTERVAL_SECONDS",
    "JobTracker",
    "register_tracked_handler",
]

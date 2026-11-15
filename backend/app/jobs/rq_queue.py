"""RQ (Redis) job backend.

The queue protocol in ``app.jobs.queue`` has two implementations: the in-process runner used by
the local profile, and this one, used when ``NEXUS_JOBS__BACKEND=rq`` and a Redis server exists.

Why a thin adapter rather than RQ everywhere?

* RQ is synchronous and process-oriented; NEXUS's handlers are async. The adapter enqueues a small
  sync dispatcher (``app.workers.tasks.run_tracked_job``) which runs the async handler in its own
  event loop.
* Job state that users care about lives in ``background_jobs`` (see the task manager), not in
  Redis. Redis is only the scheduling mechanism, so losing Redis loses no history.

The adapter is tested against ``fakeredis`` so its status mapping and cancellation behaviour are
exercised without a server. Actual worker execution against a real Redis is only available through
Docker and is listed as unverified in ``docs/ROADMAP.md`` until Phase 7 runs it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, NotFoundError
from app.core.logging import get_logger
from app.jobs.queue import JobStatusSnapshot

logger = get_logger(__name__)

#: RQ status (as returned by ``Job.get_status()``) -> NEXUS status.
_STATUS_MAP: dict[str, str] = {
    "queued": "queued",
    "deferred": "queued",
    "scheduled": "queued",
    "started": "running",
    "busy": "running",
    "finished": "completed",
    "failed": "failed",
    "stopped": "cancelled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}


def _status_name(job: Any) -> str:
    """Return an RQ job status as a plain lowercase string.

    ``rq`` returns a ``Status`` enum; ``str(Status.QUEUED)`` is ``"Status.QUEUED"``, not
    ``"queued"``, so mapping it directly silently produced ``unknown`` for every job. Reading
    ``.value`` first is what makes the mapping work across rq versions that return either a
    string or an enum.
    """
    status = job.get_status(refresh=True)
    value = getattr(status, "value", status)
    return str(value or "").lower()


class RqJobQueue:
    """Schedules jobs through RQ; the durable state stays in the database."""

    def __init__(self, settings: Settings, *, connection: Any | None = None) -> None:
        try:
            import redis  # noqa: PLC0415 - optional dependency, imported on demand
            from rq import Queue  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on optional installs
            raise ConfigurationError(
                "The RQ backend needs the 'queue' extra. Install it with: uv sync --extra queue"
            ) from exc

        self._settings = settings
        self._redis = connection or redis.Redis.from_url(settings.jobs.redis_url)
        self._queue = Queue(settings.jobs.queue_name, connection=self._redis)

    async def enqueue(
        self, job_type: str, payload: dict[str, Any] | None = None, **options: Any
    ) -> str:
        """Enqueue a dispatcher invocation and return the RQ job id."""
        from app.workers.tasks import run_tracked_job  # noqa: PLC0415 - avoids a cycle

        safe_payload = dict(payload or {})

        def _enqueue() -> str:
            job = self._queue.enqueue(
                run_tracked_job,
                job_type,
                safe_payload,
                job_timeout=int(options.get("job_timeout", 3600)),
                result_ttl=3600,
                failure_ttl=86400,
            )
            return job.id

        # redis-py is blocking; a thread keeps the event loop responsive.
        return await asyncio.to_thread(_enqueue)

    async def get_status(self, job_id: str) -> JobStatusSnapshot:
        """Return the queue's view of a job.

        Raises:
            NotFoundError: RQ does not know the id (expired result, or a different queue).
        """

        def _status() -> JobStatusSnapshot:
            from rq.job import Job  # noqa: PLC0415

            try:
                job = Job.fetch(job_id, connection=self._redis)
            except Exception as exc:  # noqa: BLE001 - rq raises several types for "missing"
                raise NotFoundError(
                    f"No RQ job with id {job_id!r}.", details={"job_id": job_id}
                ) from exc
            raw = _status_name(job)
            return JobStatusSnapshot(
                job_id=job_id,
                job_type=str(job.args[0]) if job.args else "unknown",
                status=_STATUS_MAP.get(raw, "unknown"),
                progress_current=0,
                progress_total=0,
                message=None,
            )

        return await asyncio.to_thread(_status)

    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running RQ job. Returns ``False`` when it already finished."""

        def _cancel() -> bool:
            from rq.job import Job  # noqa: PLC0415

            try:
                job = Job.fetch(job_id, connection=self._redis)
            except Exception:  # noqa: BLE001
                return False
            raw = _status_name(job)
            if _STATUS_MAP.get(raw) in {"completed", "failed", "cancelled"}:
                return False
            job.cancel()
            return True

        return await asyncio.to_thread(_cancel)

    async def shutdown(self) -> None:
        """Release the Redis connection pool."""
        close = getattr(self._redis, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("Redis connection close failed", extra={"module_name": "jobs"})


__all__ = ["RqJobQueue"]

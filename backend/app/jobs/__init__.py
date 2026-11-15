"""Background job subsystem.

The brief specifies Redis + RQ. Phase 1 ships the **abstraction** plus an in-process
implementation, because the Docker daemon is not running on the development machine (see
docs/TECHNICAL_ASSESSMENT.md Risk R2) and a foundation phase must be runnable and testable
today.

Phase 5 adds :class:`~app.jobs.queue.RqJobQueue` behind the same protocol, and nothing that
calls ``enqueue`` has to change - that is the whole point of Decision D2.
"""

from app.jobs.queue import (
    InProcessJobQueue,
    JobContext,
    JobQueue,
    JobStatusSnapshot,
    get_queue,
    register_handler,
    reset_queue,
)

__all__ = [
    "InProcessJobQueue",
    "JobContext",
    "JobQueue",
    "JobStatusSnapshot",
    "get_queue",
    "register_handler",
    "reset_queue",
]

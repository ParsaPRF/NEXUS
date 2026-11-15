"""The sync dispatcher RQ calls.

RQ executes synchronous functions in a worker process; NEXUS's handlers are async. This module is
the bridge: it initialises the database engine in the worker, builds a job context, and runs the
handler in a fresh event loop.

Using ``asyncio.run`` here is correct precisely because it is a *different* process from the API:
there is no loop to conflict with, and the handler's own sessions are created inside it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.engine import dispose_engine, get_engine, init_engine
from app.jobs.queue import JobContext, get_handler

logger = get_logger(__name__)


def run_tracked_job(
    job_type: str, payload: dict[str, Any], settings: Settings | None = None
) -> dict[str, Any]:
    """Run one job to completion. Returns the handler's result.

    Args:
        job_type: Registered handler name.
        payload: Job arguments, including ``job_db_id`` when the job is tracked.
        settings: Override for tests; defaults to the process settings.
    """
    resolved = settings or get_settings()

    # A worker may run several jobs; the engine is initialised once and reused.
    try:
        get_engine()
        owns_engine = False
    except RuntimeError:
        owns_engine = True
    init_engine(resolved)

    handler = get_handler(job_type)
    job_id = str(payload.get("job_db_id") or "")

    async def _run() -> dict[str, Any]:
        context = JobContext(
            job_id=job_id,
            job_type=job_type,
            payload=payload,
            settings=resolved,
        )
        try:
            result = await handler(context, payload)
        finally:
            if owns_engine:
                await dispose_engine()
        return result if isinstance(result, dict) else {}

    logger.info(
        "Worker running %s (%s)",
        job_type,
        job_id,
        extra={"module_name": "workers", "job_id": job_id},
    )
    return asyncio.run(_run())


__all__ = ["run_tracked_job"]

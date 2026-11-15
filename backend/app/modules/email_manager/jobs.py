"""Background job handlers for the Email Manager.

Two jobs:

* ``inbox.cleanup`` - expire overdue inboxes (status change, no data loss).
* ``inbox.poll_all`` - ingest new mail for every active inbox.

Both open their own database session, because a background job has no request to borrow one from.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.engine import get_session_factory
from app.jobs.queue import JobContext
from app.modules.task_manager.tracker import register_tracked_handler

logger = get_logger(__name__)


def _build_service(session: Any, settings: Any) -> Any:
    """Construct an InboxService inside a job (mirrors the router factory)."""
    from app.modules.email_manager.service import InboxService

    return InboxService(session, settings)


async def run_inbox_cleanup(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Expire overdue inboxes.

    Payload: ``{}`` (no arguments). Returns ``{"expired": n}``.
    """
    settings = context.settings or get_settings()
    context.set_total(1)
    factory = get_session_factory()
    async with factory() as session:
        service = _build_service(session, settings)
        expired = await service.cleanup_expired()
    context.set_progress(1, message=f"expired {expired} inboxes")
    logger.info(
        "Inbox cleanup expired %d inboxes",
        expired,
        extra={"module_name": "email_manager", "job_id": context.job_id},
    )
    return {"expired": expired}


async def run_inbox_poll_all(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Poll every active inbox for new mail.

    Payload: ``{"limit": 100}`` (optional). Returns counts, so a sweep that partially failed is
    visible rather than reported as a clean success.
    """
    settings = context.settings or get_settings()
    limit = int(payload.get("limit", 100))
    context.set_total(limit)

    factory = get_session_factory()
    async with factory() as session:
        service = _build_service(session, settings)
        # A cancellation point before the sweep starts: there is nothing to lose yet.
        context.check_cancelled()
        summary = await service.poll_all_active(limit=limit)

    context.set_progress(limit, message=f"polled {summary['inboxes']} inboxes")
    logger.info(
        "Inbox poll swept %d inboxes, ingested %d messages",
        summary["inboxes"],
        summary["ingested"],
        extra={"module_name": "email_manager", "job_id": context.job_id},
    )
    return summary


#: Registered at import time. ``app.modules.email_manager`` is imported by ``app.api.router``, so
#: these exist before any request can enqueue them.
register_tracked_handler("inbox.cleanup", run_inbox_cleanup)
register_tracked_handler("inbox.poll_all", run_inbox_poll_all)

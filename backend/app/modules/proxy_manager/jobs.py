"""Background job handlers for the Proxy Manager.

Handlers are plain async functions taking ``(JobContext, payload)``. They own their database
session - a background job has no request to borrow one from - and they report progress and check
for cancellation between units of work.

The batch handler processes proxies in small chunks rather than one giant pass. That gives three
things at once: progress the UI can display, a cancellation point that does not require killing a
transaction mid-flight, and bounded memory.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.engine import get_session_factory
from app.jobs.queue import JobContext
from app.modules.task_manager.tracker import register_tracked_handler

logger = get_logger(__name__)

#: How many proxies are checked per progress/cancellation checkpoint.
CHUNK_SIZE = 10


def _build_service(session: Any, settings: Any) -> Any:
    """Construct a ProxyService inside a job, mirroring the router's factory.

    Duplicated from the router on purpose: a background job must not depend on FastAPI's
    dependency graph, and importing the router here would drag the web layer into the worker.
    """
    from app.core.security import CredentialCipher
    from app.modules.proxy_manager.service import ProxyService

    cipher = (
        CredentialCipher(settings.proxy_credential_key) if settings.proxy_credential_key else None
    )
    return ProxyService(session, settings, cipher=cipher)


async def run_proxy_check_batch(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Check a list of proxies, reporting progress after every chunk.

    Payload:
        ``{"proxy_ids": [1, 2, 3]}``

    Returns:
        ``{"checked": n, "failed": m, "cancelled": bool}``

    A cancelled job returns normally with ``cancelled: true`` rather than raising, so the queue
    records a completed pass that stopped early and the caller can see how far it got. Genuine
    cancellation raised by ``check_cancelled`` is handled by the queue and marks the job
    ``cancelled``.
    """
    raw_ids = payload.get("proxy_ids") or []
    proxy_ids = [int(item) for item in raw_ids]
    if not proxy_ids:
        return {"checked": 0, "failed": 0, "cancelled": False}

    settings = context.settings or get_settings()
    context.set_total(len(proxy_ids))
    checked = 0
    failed = 0

    factory = get_session_factory()
    async with factory() as session:
        service = _build_service(session, settings)
        for start in range(0, len(proxy_ids), CHUNK_SIZE):
            # A cancellation point between chunks: the previous chunk is already committed, so
            # stopping here loses no completed work.
            context.check_cancelled()
            chunk = proxy_ids[start : start + CHUNK_SIZE]
            results = await service.check_by_ids(chunk)
            checked += len(results)
            failed += sum(1 for result in results if not result.ok)
            context.set_progress(
                checked, message=f"checked {checked}/{len(proxy_ids)} ({failed} failed)"
            )

    logger.info(
        "Batch check finished: %d checked, %d failed",
        checked,
        failed,
        extra={"module_name": "proxy_manager", "job_id": context.job_id},
    )
    return {"checked": checked, "failed": failed, "cancelled": False}


#: Register at import time. ``app.modules.proxy_manager`` is imported by ``app.api.router``, so
#: the handler exists before any request can enqueue this job type.
register_tracked_handler("proxy.check_batch", run_proxy_check_batch)

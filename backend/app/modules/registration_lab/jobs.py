"""Background job handler for a registration campaign.

The handler owns its database session (a job has no request), builds the executor, and ensures a
failure leaves the campaign in a terminal state rather than stuck at ``running`` forever - a stuck
campaign is invisible in the UI and blocks nothing less than retrying it.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.engine import get_session_factory
from app.jobs.queue import JobContext, register_handler
from app.models.enums import CampaignStatus

logger = get_logger(__name__)


async def run_campaign(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one campaign.

    Payload:
        ``{"campaign_id": int, "email_timeout_seconds": int}``
    """
    from app.modules.registration_lab.executor import CampaignExecutor
    from app.modules.registration_lab.repository import RegistrationRepository

    settings = context.settings or get_settings()
    campaign_id = int(payload["campaign_id"])
    email_timeout = float(payload.get("email_timeout_seconds", 30))

    factory = get_session_factory()
    async with factory() as session:
        repo = RegistrationRepository(session)
        campaign = await repo.get_campaign(campaign_id)
        if campaign is None:
            logger.error(
                "Campaign %s vanished before it could run",
                campaign_id,
                extra={"module_name": "registration_lab", "job_id": context.job_id},
            )
            return {"campaign_id": campaign_id, "status": "missing"}

        target = await repo.get_target(campaign.target_id)
        try:
            if target is None:
                msg = f"Campaign {campaign_id} has no target"
                raise RuntimeError(msg)
            executor = CampaignExecutor(
                session,
                settings,
                campaign,
                target,
                email_timeout_seconds=email_timeout,
            )
            return await executor.run(context)
        except Exception as exc:
            # A crash must not leave the campaign 'running' forever.
            await session.rollback()
            campaign = await repo.get_campaign(campaign_id)
            if campaign is not None and campaign.status == CampaignStatus.RUNNING.value:
                campaign.status = CampaignStatus.FAILED.value
                campaign.stop_reason = f"execution failed: {type(exc).__name__}"[:255]
                await session.commit()
            logger.exception(
                "Campaign %s failed",
                campaign_id,
                extra={"module_name": "registration_lab", "job_id": context.job_id},
            )
            raise


register_handler("campaign.run", run_campaign)

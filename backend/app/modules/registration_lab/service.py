"""Registration Lab service: targets, campaigns and results.

Rules enforced here:

* a target can only be created after its URL passes the policy check (:mod:`authorization`);
* a campaign can only be created for a target that currently passes **both** authorization gates;
* identities must be addresses this instance can receive mail for - imported addresses are checked
  against the domains NEXUS hosts, and generated ones come from real inboxes;
* a target may be authorized later, but only with a written note, and the action is audited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.models.enums import CampaignStatus, ResultOutcome
from app.models.registration_models import (
    Campaign,
    RegistrationTarget,
    RegistrationTest,
)
from app.modules.email_manager.service import InboxService
from app.modules.registration_lab.authorization import (
    ensure_target_usable,
    validate_target_url,
)
from app.modules.registration_lab.identities import (
    MAX_IDENTITIES_PER_CAMPAIGN,
    domain_of,
    parse_identity_text,
)
from app.modules.registration_lab.repository import CampaignQuery, RegistrationRepository
from app.modules.registration_lab.schemas import (
    CampaignRead,
    CampaignResultsResponse,
    RegistrationResultRead,
)
from app.services.audit_service import AuditService


class RegistrationService:
    """Use cases for targets, campaigns and results."""

    def __init__(
        self, session: Any, settings: Settings, *, inbox_service: InboxService | None = None
    ) -> None:
        self._session = session
        self._settings = settings
        self._repo = RegistrationRepository(session)
        self._inboxes = inbox_service or InboxService(session, settings)
        self._audit = AuditService(session, enabled=settings.audit_log_enabled)

    # ------------------------------------------------------------------ targets

    async def create_target(
        self,
        *,
        name: str,
        base_url: str,
        kind: str = "demo",
        adapter_name: str = "demo_api",
        is_authorized: bool = False,
        authorization_note: str | None = None,
        request: Any | None = None,
    ) -> RegistrationTarget:
        """Register a target.

        Raises:
            ConflictError: the base URL is already registered.
            TargetNotAuthorizedError: the URL is not permitted by policy.
        """
        validate_target_url(base_url, kind=kind, settings=self._settings)
        if await self._repo.get_target_by_url(base_url) is not None:
            raise ConflictError(f"Target {base_url!r} is already registered.")
        if is_authorized and not authorization_note:
            raise ValidationError(
                "An authorized target needs an authorization note recording who approved it.",
                details={"field": "authorization_note"},
            )

        target = await self._repo.add_target(
            RegistrationTarget(
                name=name,
                base_url=base_url,
                kind=kind,
                adapter_name=adapter_name,
                is_authorized=is_authorized,
                authorization_note=authorization_note,
                is_active=True,
            )
        )
        await self._audit.record(
            "target.create",
            entity_type="registration_target",
            entity_id=target.id,
            detail={"base_url": base_url, "kind": kind, "is_authorized": is_authorized},
            request_id=_request_id(request),
        )
        await self._session.commit()
        return target

    async def authorize_target(
        self, target_id: int, *, note: str, request: Any | None = None
    ) -> RegistrationTarget:
        """Mark a target authorized, recording the note.

        Raises:
            NotFoundError: no target has that id.
            ValidationError: the note is empty.
        """
        target = await self._repo.get_target(target_id)
        if target is None:
            raise NotFoundError(
                f"Target {target_id} was not found.", details={"target_id": target_id}
            )
        if not note.strip():
            raise ValidationError("An authorization note is required.", details={"field": "note"})

        target.is_authorized = True
        target.authorization_note = note.strip()
        await self._audit.record(
            "target.authorize",
            entity_type="registration_target",
            entity_id=target.id,
            detail={"note": note.strip()},
            request_id=_request_id(request),
            strict=True,
        )
        await self._session.commit()
        return target

    async def list_targets(self) -> list[RegistrationTarget]:
        return await self._repo.list_targets()

    async def get_target(self, target_id: int) -> RegistrationTarget:
        target = await self._repo.get_target(target_id)
        if target is None:
            raise NotFoundError(
                f"Target {target_id} was not found.", details={"target_id": target_id}
            )
        return target

    # ------------------------------------------------------------------ campaigns

    async def create_campaign(
        self,
        *,
        name: str,
        target_id: int,
        count: int = 0,
        identities_text: str | None = None,
        concurrency: int = 1,
        rate_limit_per_minute: int = 20,
        email_timeout_seconds: int = 30,
        request: Any | None = None,
    ) -> tuple[Campaign, str]:
        """Create a campaign and queue it.

        Returns:
            The campaign and the background job id that will run it.

        Raises:
            NotFoundError: the target does not exist.
            TargetNotAuthorizedError: the target fails an authorization gate.
            ValidationError: no identities were supplied, or one is not receivable.
        """
        target = await self.get_target(target_id)
        ensure_target_usable(target, self._settings)

        addresses, inboxes = await self._resolve_identities(
            name=name, count=count, identities_text=identities_text
        )

        campaign = await self._repo.add_campaign(
            Campaign(
                public_id=str(uuid.uuid4()),
                name=name,
                target_id=target.id,
                status=CampaignStatus.QUEUED.value,
                total_tests=len(addresses),
                concurrency=concurrency,
                rate_limit_per_minute=rate_limit_per_minute,
                scenario_config_json={"email_timeout_seconds": email_timeout_seconds},
            )
        )
        for address, inbox_public_id in zip(addresses, inboxes, strict=True):
            inbox = await self._inboxes.get_inbox(inbox_public_id)
            await self._repo.add_test(
                RegistrationTest(
                    campaign_id=campaign.id,
                    inbox_id=inbox.id,
                    email=address,
                    password_ref=f"campaign:{campaign.id}:generated",
                    status="pending",
                )
            )

        await self._audit.record(
            "campaign.create",
            entity_type="campaign",
            entity_id=campaign.id,
            detail={
                "target_id": target.id,
                "identities": len(addresses),
                "rate_limit_per_minute": rate_limit_per_minute,
                "concurrency": concurrency,
            },
            request_id=_request_id(request),
        )
        await self._session.commit()

        from app.jobs.queue import get_queue

        job_id = await get_queue(self._settings).enqueue(
            "campaign.run",
            {"campaign_id": campaign.id, "email_timeout_seconds": email_timeout_seconds},
            progress_total=len(addresses),
        )
        return campaign, job_id

    async def _resolve_identities(
        self, *, name: str, count: int, identities_text: str | None
    ) -> tuple[list[str], list[str]]:
        """Turn the request into (addresses, inbox public ids).

        Imported addresses must sit on a domain this instance hosts; otherwise the verification
        email could never be read, and the campaign would fail with a misleading timeout.
        """
        if identities_text:
            addresses = parse_identity_text(identities_text)
            hosted = {
                domain.domain.lower()
                for domain in await self._inboxes.list_domains()
                if domain.is_active and (domain.is_reserved or domain.owner_verified)
            }
            foreign = sorted({domain_of(address) for address in addresses} - hosted)
            if foreign:
                raise ValidationError(
                    "Imported identities must be addresses on a domain this instance hosts, "
                    "otherwise their verification email cannot be read.",
                    details={"unsupported_domains": foreign},
                )
            inboxes = [
                (
                    await self._inboxes.ensure_inbox_for_address(address, label=f"campaign {name}")
                ).public_id
                for address in addresses
            ]
            return addresses, inboxes

        if count <= 0:
            raise ValidationError(
                "Provide either count to generate identities, or identities_text to import them."
            )
        if count > MAX_IDENTITIES_PER_CAMPAIGN:
            raise ValidationError(
                f"A campaign may contain at most {MAX_IDENTITIES_PER_CAMPAIGN} identities."
            )

        addresses = []
        inboxes = []
        for _ in range(count):
            inbox = await self._inboxes.create_inbox(label=f"campaign {name}")
            addresses.append(inbox.address)
            inboxes.append(inbox.public_id)
        return addresses, inboxes

    async def list_campaigns(self, query: CampaignQuery) -> tuple[list[Campaign], int]:
        return await self._repo.list_campaigns(query)

    async def get_campaign(self, public_id: str) -> Campaign:
        campaign = await self._repo.get_campaign_by_public_id(public_id)
        if campaign is None:
            raise NotFoundError(
                f"Campaign {public_id!r} was not found.", details={"public_id": public_id}
            )
        return campaign

    async def cancel_campaign(self, public_id: str, *, request: Any | None = None) -> Campaign:
        """Request cancellation.

        The executor checks this status before starting each identity, so cancelling stops new
        work; a test already in flight finishes and is recorded rather than being torn apart.
        """
        campaign = await self.get_campaign(public_id)
        if campaign.status in {
            CampaignStatus.COMPLETED.value,
            CampaignStatus.FAILED.value,
            CampaignStatus.CANCELLED.value,
            CampaignStatus.STOPPED_BY_TARGET.value,
        }:
            raise ConflictError(f"Campaign is already {campaign.status}.")
        campaign.status = CampaignStatus.CANCELLED.value
        campaign.stop_reason = "cancelled by operator"
        await self._audit.record(
            "campaign.cancel",
            entity_type="campaign",
            entity_id=campaign.id,
            request_id=_request_id(request),
        )
        await self._session.commit()
        return campaign

    async def results(
        self, public_id: str, *, limit: int = 200, offset: int = 0
    ) -> CampaignResultsResponse:
        """Return per-test results plus a summary split into defects and controls."""
        from app.schemas.common import PageMeta

        campaign = await self.get_campaign(public_id)
        rows, total = await self._repo.results_for_campaign(campaign.id, limit=limit, offset=offset)
        items = [
            RegistrationResultRead(
                test_id=test.id,
                email=test.email,
                status=test.status,
                outcome=result.outcome if result else None,
                verification_code=result.verification_code if result else None,
                code_extraction_confidence=result.code_extraction_confidence if result else None,
                account_status=result.account_status if result else None,
                duration_ms=result.duration_ms if result else None,
                error_message=test.error_message,
                steps=result.steps_json if result else None,
            )
            for test, result in rows
        ]
        return CampaignResultsResponse(
            campaign=_campaign_read(campaign),
            items=items,
            meta=PageMeta(total=total, limit=limit, offset=offset),
            summary=_summarize(items),
        )


def _summarize(items: list[RegistrationResultRead]) -> dict[str, int]:
    """Count outcomes, keeping defects and controls visibly separate."""
    summary: dict[str, int] = {}
    defects = 0
    controls = 0
    for item in items:
        if item.outcome is None:
            summary["pending"] = summary.get("pending", 0) + 1
            continue
        summary[item.outcome] = summary.get(item.outcome, 0) + 1
        if item.outcome in {outcome.value for outcome in ResultOutcome.terminal_control_outcomes()}:
            controls += 1
        elif item.outcome != ResultOutcome.SUCCESS.value:
            defects += 1
    summary["product_defects"] = defects
    summary["controls_encountered"] = controls
    return summary


def _campaign_read(campaign: Campaign) -> CampaignRead:
    """Map a campaign row to its API schema."""
    return CampaignRead(
        public_id=campaign.public_id,
        name=campaign.name,
        target_id=campaign.target_id,
        status=campaign.status,
        total_tests=campaign.total_tests,
        completed_tests=campaign.completed_tests,
        succeeded_tests=campaign.succeeded_tests,
        failed_tests=campaign.failed_tests,
        progress_percent=(
            0.0
            if not campaign.total_tests
            else round(campaign.completed_tests / campaign.total_tests * 100, 2)
        ),
        concurrency=campaign.concurrency,
        rate_limit_per_minute=campaign.rate_limit_per_minute,
        started_at=campaign.started_at,
        finished_at=campaign.finished_at,
        stop_reason=campaign.stop_reason,
        created_at=campaign.created_at or datetime.now(UTC),
    )


def _request_id(request: Any | None) -> str | None:
    return getattr(getattr(request, "state", None), "request_id", None)


__all__ = ["RegistrationService"]

"""Registration Lab HTTP routes.

Reads use any authenticated principal; creating targets/campaigns and authorizing targets require
the admin role.

The routes are thin: authorization, identity resolution and execution live in the service and the
executor, so the same use cases are available to the background job without going through HTTP.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.config import Settings, get_settings
from app.core.security import Principal, get_principal, require_admin
from app.database.engine import DbSession
from app.models.enums import CampaignStatus
from app.modules.registration_lab.repository import CampaignQuery
from app.modules.registration_lab.schemas import (
    CampaignCreate,
    CampaignCreateResponse,
    CampaignListResponse,
    CampaignRead,
    CampaignResultsResponse,
    TargetCreate,
    TargetRead,
)
from app.modules.registration_lab.service import RegistrationService, _campaign_read
from app.schemas.common import PageMeta

router = APIRouter(tags=["registration"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
AdminDep = Annotated[Principal, Depends(require_admin)]
ReaderDep = Annotated[Principal, Depends(get_principal)]


def get_registration_service(session: DbSession, settings: SettingsDep) -> RegistrationService:
    """Build the service for this request."""
    return RegistrationService(session, settings)


ServiceDep = Annotated[RegistrationService, Depends(get_registration_service)]


def _target_read(target) -> TargetRead:  # type: ignore[no-untyped-def]
    """Map a target row to its schema."""
    return TargetRead(
        id=target.id,
        name=target.name,
        base_url=target.base_url,
        kind=target.kind,
        adapter_name=target.adapter_name,
        is_authorized=bool(target.is_authorized),
        authorization_note=target.authorization_note,
        is_active=bool(target.is_active),
        may_be_tested=target.may_be_tested,
        created_at=target.created_at,
    )


# --------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------


@router.get("/targets", response_model=list[TargetRead], summary="List registration targets")
async def list_targets(service: ServiceDep, principal: ReaderDep) -> list[TargetRead]:
    """Return every registered target.

    ``may_be_tested`` is a convenience flag for the UI; the executor re-checks the real gates
    before running anything.
    """
    return [_target_read(target) for target in await service.list_targets()]


@router.post(
    "/targets",
    response_model=TargetRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a target",
)
async def create_target(
    payload: TargetCreate, request: Request, service: ServiceDep, principal: AdminDep
) -> TargetRead:
    """Register a registration target.

    The URL is validated at creation time (SSRF guard + policy), so a target that could never be
    used cannot be stored. ``kind: demo`` targets must be local - the bundled demonstration
    application is the only target allowed without an external allowlist entry.
    """
    target = await service.create_target(
        name=payload.name,
        base_url=payload.base_url,
        kind=payload.kind,
        adapter_name=payload.adapter_name,
        is_authorized=payload.is_authorized,
        authorization_note=payload.authorization_note,
        request=request,
    )
    return _target_read(target)


@router.get("/targets/{target_id}", response_model=TargetRead, summary="Get one target")
async def get_target(target_id: int, service: ServiceDep, principal: ReaderDep) -> TargetRead:
    """Return one target by id."""
    return _target_read(await service.get_target(target_id))


@router.post(
    "/targets/{target_id}/authorize",
    response_model=TargetRead,
    summary="Authorize a target",
)
async def authorize_target(
    target_id: int,
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
    note: Annotated[str, Query(min_length=3, max_length=2000)] = "",
) -> TargetRead:
    """Record that testing this target is authorized, and by whom.

    A campaign cannot run until this has been done: the flag defaults to false and the note is
    mandatory, because "someone said it was fine" is not a record.
    """
    target = await service.authorize_target(target_id, note=note, request=request)
    return _target_read(target)


# --------------------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------------------


@router.get("/campaigns", response_model=CampaignListResponse, summary="List campaigns")
async def list_campaigns(
    service: ServiceDep,
    principal: ReaderDep,
    statuses: Annotated[list[CampaignStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CampaignListResponse:
    """Return a page of campaigns, newest first."""
    query = CampaignQuery(
        statuses=[item.value for item in statuses or []], limit=limit, offset=offset
    )
    campaigns, total = await service.list_campaigns(query)
    return CampaignListResponse(
        items=[_campaign_read(campaign) for campaign in campaigns],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.post(
    "/campaigns",
    response_model=CampaignCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create and queue a campaign",
)
async def create_campaign(
    payload: CampaignCreate, request: Request, service: ServiceDep, principal: AdminDep
) -> CampaignCreateResponse:
    """Create a campaign and queue it.

    Either ``count`` generated identities or ``identities_text`` imported addresses. Imported
    addresses must be on a domain this instance hosts, or their verification email could not be
    read.
    """
    campaign, job_id = await service.create_campaign(
        name=payload.name,
        target_id=payload.target_id,
        count=payload.count,
        identities_text=payload.identities_text,
        concurrency=payload.concurrency,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        email_timeout_seconds=payload.email_timeout_seconds,
        request=request,
    )
    return CampaignCreateResponse(campaign=_campaign_read(campaign), job_id=job_id)


@router.get("/campaigns/{public_id}", response_model=CampaignRead, summary="Get one campaign")
async def get_campaign(public_id: str, service: ServiceDep, principal: ReaderDep) -> CampaignRead:
    """Return one campaign and its progress."""
    return _campaign_read(await service.get_campaign(public_id))


@router.get(
    "/campaigns/{public_id}/results",
    response_model=CampaignResultsResponse,
    summary="Campaign results",
)
async def campaign_results(
    public_id: str,
    service: ServiceDep,
    principal: ReaderDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CampaignResultsResponse:
    """Return per-test outcomes and a summary.

    The summary separates ``product_defects`` from ``controls_encountered``: one is a bug report,
    the other a boundary the tool respected.
    """
    return await service.results(public_id, limit=limit, offset=offset)


@router.post(
    "/campaigns/{public_id}/cancel",
    response_model=CampaignRead,
    summary="Cancel a campaign",
)
async def cancel_campaign(
    public_id: str, request: Request, service: ServiceDep, principal: AdminDep
) -> CampaignRead:
    """Request cancellation.

    The runner stops before starting the next identity; a test already in flight finishes and is
    recorded, rather than being interrupted mid-transaction.
    """
    return _campaign_read(await service.cancel_campaign(public_id, request=request))

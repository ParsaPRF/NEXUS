"""Email Manager HTTP routes.

Reads are open to any authenticated principal; creating/deleting inboxes and triggering polls
require the admin role.

Static paths are declared before ``/{public_id}`` so that ``/inboxes/bulk`` and
``/inboxes/cleanup`` are not captured as inbox identifiers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.config import Settings, get_settings
from app.core.security import Principal, get_principal, require_admin
from app.database.engine import DbSession
from app.models.enums import InboxStatus
from app.modules.email_manager.repository import InboxQuery
from app.modules.email_manager.schemas import (
    CodeExtractionRead,
    EmailDomainCreate,
    EmailDomainRead,
    InboxBulkCreate,
    InboxBulkResponse,
    InboxCleanupResponse,
    InboxCreate,
    InboxListResponse,
    InboxPollResponse,
    InboxRead,
    MessageListResponse,
    MessageRead,
)
from app.modules.email_manager.service import MAX_EXPORT_MESSAGES, InboxService
from app.schemas.common import PageMeta

router = APIRouter(tags=["email"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
AdminDep = Annotated[Principal, Depends(require_admin)]
ReaderDep = Annotated[Principal, Depends(get_principal)]


def get_inbox_service(session: DbSession, settings: SettingsDep) -> InboxService:
    """Build the service for this request."""
    return InboxService(session, settings)


ServiceDep = Annotated[InboxService, Depends(get_inbox_service)]


# --------------------------------------------------------------------------------------
# Domains
# --------------------------------------------------------------------------------------


@router.get("/email-domains", response_model=list[EmailDomainRead], summary="List inbox domains")
async def list_domains(service: ServiceDep, principal: ReaderDep) -> list[EmailDomainRead]:
    """Return configured domains.

    A reserved domain (``example.test``) is created on demand the first time an inbox is made;
    this list is how a client discovers it.
    """
    return await service.list_domains()


@router.post(
    "/email-domains",
    response_model=EmailDomainRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register an inbox domain",
)
async def create_domain(
    payload: EmailDomainCreate,
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
) -> EmailDomainRead:
    """Register a domain for test inboxes.

    A domain outside the RFC-reserved TLDs requires ``owner_verified=true``: NEXUS will not host
    test inboxes on a domain the operator may not control.
    """
    return await service.create_domain(
        payload.domain,
        owner_verified=payload.owner_verified,
        description=payload.description,
        request=request,
    )


# --------------------------------------------------------------------------------------
# Inboxes - collection and static routes (before /{public_id})
# --------------------------------------------------------------------------------------


@router.get("/inboxes", response_model=InboxListResponse, summary="List inboxes")
async def list_inboxes(
    service: ServiceDep,
    principal: ReaderDep,
    statuses: Annotated[list[InboxStatus] | None, Query(alias="status")] = None,
    campaign_id: int | None = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    expiring_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InboxListResponse:
    """Return a filtered page of inboxes, newest first."""
    query = InboxQuery(
        statuses=[item.value for item in statuses or []],
        campaign_id=campaign_id,
        search=search,
        expiring_only=expiring_only,
        limit=limit,
        offset=offset,
    )
    items, total = await service.list_inboxes(query)
    return InboxListResponse(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))


@router.post(
    "/inboxes",
    response_model=InboxRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a test inbox",
)
async def create_inbox(
    payload: InboxCreate,
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
) -> InboxRead:
    """Create one inbox with a random alias (or a requested one)."""
    return await service.create_inbox(
        label=payload.label,
        ttl_hours=payload.ttl_hours,
        local_part=payload.local_part,
        request=request,
    )


@router.post(
    "/inboxes/bulk",
    response_model=InboxBulkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create several test inboxes",
)
async def bulk_create_inboxes(
    payload: InboxBulkCreate,
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
) -> InboxBulkResponse:
    """Create several inboxes at once.

    Capped by ``NEXUS_MAIL__MAX_BULK_INBOXES``. The cap is a safety limit, not a performance
    tuning knob: bulk creation exists for test setup, not for generating mailboxes at scale.
    """
    return await service.bulk_create_inboxes(
        count=payload.count,
        label_prefix=payload.label_prefix,
        ttl_hours=payload.ttl_hours,
        request=request,
    )


@router.post(
    "/inboxes/cleanup",
    response_model=InboxCleanupResponse,
    summary="Expire overdue inboxes",
)
async def cleanup_inboxes(
    request: Request, service: ServiceDep, principal: AdminDep
) -> InboxCleanupResponse:
    """Run an expiry sweep immediately.

    The same sweep also runs as a background job; this endpoint exists so the behaviour can be
    triggered and observed without waiting for a scheduler.
    """
    expired = await service.cleanup_expired(request=request)
    return InboxCleanupResponse(expired=expired)


# --------------------------------------------------------------------------------------
# Inboxes - one item
# --------------------------------------------------------------------------------------


@router.get("/inboxes/{public_id}", response_model=InboxRead, summary="Get one inbox")
async def get_inbox(public_id: str, service: ServiceDep, principal: ReaderDep) -> InboxRead:
    """Return one inbox by its public id."""
    return await service.get_inbox_read(public_id)


@router.delete(
    "/inboxes/{public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an inbox",
)
async def delete_inbox(
    public_id: str, request: Request, service: ServiceDep, principal: AdminDep
) -> Response:
    """Delete an inbox and every message it received."""
    await service.delete_inbox(public_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/inboxes/{public_id}/poll",
    response_model=InboxPollResponse,
    summary="Ingest new messages",
)
async def poll_inbox(
    public_id: str, request: Request, service: ServiceDep, principal: AdminDep
) -> InboxPollResponse:
    """Fetch new messages for this inbox from the configured mail backend.

    Ingestion is idempotent: polling repeatedly returns ``ingested: 0`` once everything is stored.
    """
    return await service.poll_inbox(public_id, request=request)


@router.get(
    "/inboxes/{public_id}/messages",
    response_model=MessageListResponse,
    summary="List an inbox's messages",
)
async def list_messages(
    public_id: str,
    service: ServiceDep,
    principal: ReaderDep,
    search: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageListResponse:
    """Return a page of messages, newest first. Bodies are omitted from the list view."""
    items, total = await service.list_messages(public_id, search=search, limit=limit, offset=offset)
    return MessageListResponse(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))


@router.get("/inboxes/{public_id}/export", summary="Export an inbox's messages")
async def export_messages(
    public_id: str,
    service: ServiceDep,
    principal: AdminDep,
    export_format: Annotated[str, Query(alias="format", pattern="^(csv|json)$")] = "csv",
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_MESSAGES)] = 1000,
) -> Response:
    """Export messages as CSV or JSON."""
    content, media_type = await service.export_messages(public_id, export_format, limit=limit)
    filename = f"nexus-inbox.{export_format}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------------------


@router.get("/messages/{message_id}", response_model=MessageRead, summary="Get one message")
async def get_message(message_id: int, service: ServiceDep, principal: ReaderDep) -> MessageRead:
    """Return one message including bodies.

    ``body_html`` is untrusted: it is returned for extraction and debugging, and a client must not
    render it as markup.
    """
    return await service.get_message(message_id)


@router.post(
    "/messages/{message_id}/extract-code",
    response_model=CodeExtractionRead,
    summary="Re-run verification-code extraction",
)
async def extract_code(
    message_id: int, service: ServiceDep, principal: AdminDep
) -> CodeExtractionRead:
    """Recompute the verification code for a message and cache the result.

    Useful after extraction rules improve, and for diagnosing a message where the code was not
    picked up.
    """
    return await service.extract_code(message_id)

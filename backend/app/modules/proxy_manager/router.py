"""Proxy Manager HTTP routes.

Conventions followed by every NEXUS router:

* Reads are available to any authenticated principal; writes require the admin role.
* Path parameters are validated by FastAPI (``proxy_id: int``), so a non-numeric id is a 422
  before any query runs.
* **Static paths are declared before dynamic ones** (``/stats``, ``/export``,
  ``/check-batch`` before ``/{proxy_id}``), otherwise ``/stats`` would be captured as a
  ``proxy_id`` and fail validation.
* The service is built per request from the request's own session and settings, so a test-built
  application is fully isolated.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.config import Settings, get_settings
from app.core.security import CredentialCipher, Principal, get_principal, require_admin
from app.database.engine import DbSession
from app.models.enums import ProxyProtocol, ProxyStatus
from app.modules.proxy_manager.repository import ProxyQuery
from app.modules.proxy_manager.schemas import (
    ProxyBatchCheckRequest,
    ProxyBatchCheckResponse,
    ProxyCheckResponse,
    ProxyImportRequest,
    ProxyImportResponse,
    ProxyListResponse,
    ProxyRead,
    ProxyStatsResponse,
    ProxyUpdateRequest,
)
from app.modules.proxy_manager.service import MAX_EXPORT_ROWS, ProxyService
from app.schemas.common import PageMeta

router = APIRouter(tags=["proxies"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
AdminDep = Annotated[Principal, Depends(require_admin)]
ReaderDep = Annotated[Principal, Depends(get_principal)]


def get_proxy_service(session: DbSession, settings: SettingsDep) -> ProxyService:
    """Build the service for this request.

    The credential cipher is created only when a key is configured. Passing ``None`` otherwise is
    deliberate: a credential-bearing proxy then produces an explicit refusal instead of a
    confusing failure later.
    """
    cipher = (
        CredentialCipher(settings.proxy_credential_key) if settings.proxy_credential_key else None
    )
    return ProxyService(session, settings, cipher=cipher)


ServiceDep = Annotated[ProxyService, Depends(get_proxy_service)]


def _query_from_request(
    *,
    statuses: list[ProxyStatus] | None = None,
    protocols: list[ProxyProtocol] | None = None,
    source_id: int | None = None,
    search: str | None = None,
    enabled_only: bool = False,
    min_latency_ms: int | None = None,
    max_latency_ms: int | None = None,
    sort: str = "-created_at",
    limit: int = 50,
    offset: int = 0,
) -> ProxyQuery:
    """Translate validated query parameters into a repository query object."""
    return ProxyQuery(
        statuses=[item.value for item in statuses or []],
        protocols=[item.value for item in protocols or []],
        source_id=source_id,
        search=search,
        enabled_only=enabled_only,
        min_latency_ms=min_latency_ms,
        max_latency_ms=max_latency_ms,
        sort=sort,
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------------------


@router.post(
    "/proxy-sources",
    response_model=ProxyImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a proxy list",
)
async def import_proxies(
    payload: ProxyImportRequest,
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
) -> ProxyImportResponse:
    """Parse a TXT or CSV proxy list and store the new proxies.

    Duplicates of already-stored endpoints are skipped and counted. Entries with a password are
    refused - not stored - when no credential encryption key is configured.
    """
    return await service.import_list(
        payload.content,
        fmt=payload.format,
        name=payload.name,
        kind=payload.kind,
        location=payload.location,
        request=request,
    )


# --------------------------------------------------------------------------------------
# Collection reads (declared before /{proxy_id})
# --------------------------------------------------------------------------------------


@router.get("/proxies", response_model=ProxyListResponse, summary="List proxies")
async def list_proxies(
    service: ServiceDep,
    principal: ReaderDep,
    statuses: Annotated[list[ProxyStatus] | None, Query(alias="status")] = None,
    protocols: Annotated[list[ProxyProtocol] | None, Query(alias="protocol")] = None,
    source_id: int | None = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    enabled_only: bool = False,
    min_latency_ms: Annotated[int | None, Query(ge=0)] = None,
    max_latency_ms: Annotated[int | None, Query(ge=0)] = None,
    sort: Annotated[str, Query(max_length=32)] = "-created_at",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProxyListResponse:
    """Return a filtered, sorted page of proxies.

    Filtering happens in SQL, not in Python: the desktop UI pages through large pools and a
    client-side filter would require transferring the whole table (threat T7).
    """
    query = _query_from_request(
        statuses=statuses,
        protocols=protocols,
        source_id=source_id,
        search=search,
        enabled_only=enabled_only,
        min_latency_ms=min_latency_ms,
        max_latency_ms=max_latency_ms,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    items, total = await service.list_proxies(query)
    return ProxyListResponse(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))


@router.get("/proxies/stats", response_model=ProxyStatsResponse, summary="Proxy statistics")
async def proxy_stats(service: ServiceDep, principal: ReaderDep) -> ProxyStatsResponse:
    """Counts used by the dashboard widgets."""
    return await service.stats()


@router.get("/proxies/export", summary="Export proxies as CSV or JSON")
async def export_proxies(
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
    export_format: Annotated[str, Query(alias="format", pattern="^(csv|json)$")] = "csv",
    statuses: Annotated[list[ProxyStatus] | None, Query(alias="status")] = None,
    protocols: Annotated[list[ProxyProtocol] | None, Query(alias="protocol")] = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    enabled_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_ROWS)] = 1000,
) -> Response:
    """Export the matching proxies.

    Credentials are never exported. An export is a file that gets copied around, so it carries
    nothing that would let someone use the proxies' accounts.
    """
    query = _query_from_request(
        statuses=statuses,
        protocols=protocols,
        search=search,
        enabled_only=enabled_only,
        limit=limit,
        sort="id",
    )
    content, media_type = await service.export(export_format, query)
    filename = f"nexus-proxies.{export_format}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/proxies/check-batch",
    response_model=ProxyBatchCheckResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a batch health check",
)
async def check_proxies_batch(
    payload: ProxyBatchCheckRequest,
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
) -> ProxyBatchCheckResponse:
    """Queue a health check for several proxies.

    Returns ``202`` with a job id: the work happens in the background, and the UI polls the job.
    Either ``proxy_ids`` or ``status_filter`` must select at least one proxy.
    """
    proxy_ids = list(payload.proxy_ids)
    if not proxy_ids and payload.status_filter is not None:
        proxy_ids = await service.resolve_batch_ids(
            status_filter=payload.status_filter.value, limit=payload.limit
        )
    return await service.request_batch_check(proxy_ids, request=request)


# --------------------------------------------------------------------------------------
# Single item
# --------------------------------------------------------------------------------------


@router.get("/proxies/{proxy_id}", response_model=ProxyRead, summary="Get one proxy")
async def get_proxy(proxy_id: int, service: ServiceDep, principal: ReaderDep) -> ProxyRead:
    """Return one proxy by id."""
    proxy = await service.get_proxy(proxy_id)
    return ProxyRead.model_validate(proxy)


@router.patch("/proxies/{proxy_id}", response_model=ProxyRead, summary="Enable or disable a proxy")
async def update_proxy(
    proxy_id: int,
    payload: ProxyUpdateRequest,
    request: Request,
    service: ServiceDep,
    principal: AdminDep,
) -> ProxyRead:
    """Enable or disable a proxy, keeping its check history."""
    return await service.set_enabled(proxy_id, enabled=payload.is_enabled, request=request)


@router.delete(
    "/proxies/{proxy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a proxy",
)
async def delete_proxy(
    proxy_id: int, request: Request, service: ServiceDep, principal: AdminDep
) -> Response:
    """Delete a proxy and its check history."""
    await service.delete_proxy(proxy_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/proxies/{proxy_id}/check",
    response_model=ProxyCheckResponse,
    summary="Check one proxy",
)
async def check_proxy(
    proxy_id: int, request: Request, service: ServiceDep, principal: AdminDep
) -> ProxyCheckResponse:
    """Run a health check against the configured check endpoint and record the result."""
    return await service.check_proxy(proxy_id, request=request)

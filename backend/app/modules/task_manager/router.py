"""Task Manager HTTP routes: job monitor and analytics.

The monitor is read-mostly; cancellation is the only state-changing operation and requires the
admin role. Analytics exports are admin-only because they can contain operational detail.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.config import Settings, get_settings
from app.core.security import Principal, get_principal, require_admin
from app.database.engine import DbSession
from app.models.enums import JobStatus
from app.modules.task_manager.schemas import (
    AnalyticsSummary,
    JobCancelResponse,
    JobDetail,
    JobListResponse,
    JobLogRead,
    JobRead,
)
from app.modules.task_manager.service import EXPORT_DATASETS, MAX_EXPORT_ROWS, JobService
from app.schemas.common import PageMeta

router = APIRouter(tags=["jobs"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
AdminDep = Annotated[Principal, Depends(require_admin)]
ReaderDep = Annotated[Principal, Depends(get_principal)]


def get_job_service(session: DbSession, settings: SettingsDep) -> JobService:
    """Build the service for this request."""
    return JobService(session, settings)


ServiceDep = Annotated[JobService, Depends(get_job_service)]


def _job_read(job) -> JobRead:  # type: ignore[no-untyped-def]
    """Map a job row to its schema, computing the percentage."""
    total = job.progress_total or 0
    current = job.progress_current or 0
    percent = 0.0 if total <= 0 else min(100.0, round(current / total * 100, 2))
    return JobRead(
        public_id=job.public_id,
        job_type=job.job_type,
        status=job.status,
        progress_current=current,
        progress_total=total,
        progress_percent=percent,
        attempts=job.attempts or 0,
        duration_ms=job.duration_ms,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_message=job.error_message,
        cancel_requested=bool(job.cancel_requested),
        idempotency_key=job.idempotency_key,
    )


# --------------------------------------------------------------------------------------
# Job monitor
# --------------------------------------------------------------------------------------


@router.get("/jobs", response_model=JobListResponse, summary="List background jobs")
async def list_jobs(
    service: ServiceDep,
    principal: ReaderDep,
    statuses: Annotated[list[JobStatus] | None, Query(alias="status")] = None,
    job_type: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    """Return a filtered page of jobs, newest first."""
    jobs, total = await service.list_jobs(
        statuses=[item.value for item in statuses or []],
        job_type=job_type,
        limit=limit,
        offset=offset,
    )
    return JobListResponse(
        items=[_job_read(job) for job in jobs],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/jobs/{public_id}", response_model=JobDetail, summary="Get one job")
async def get_job(public_id: str, service: ServiceDep, principal: ReaderDep) -> JobDetail:
    """Return one job with its logs.

    The payload is included but already redacted: a job argument may have carried a credential.
    """
    job = await service.get(public_id)
    logs = await service.logs(public_id)
    base = _job_read(job)
    return JobDetail(
        **base.model_dump(),
        logs=[
            JobLogRead(level=log.level, message=log.message, created_at=log.created_at)
            for log in logs
        ],
        payload=job.payload_json,
    )


@router.post(
    "/jobs/{public_id}/cancel",
    response_model=JobCancelResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a job",
)
async def cancel_job(
    public_id: str, request: Request, service: ServiceDep, principal: AdminDep
) -> JobCancelResponse:
    """Ask a job to stop.

    Cancellation is cooperative: the flag is set and the running job stops at its next safe point,
    so a batch already committed is never torn apart. An already-finished job returns ``409``.
    """
    job = await service.request_cancel(public_id, request=request)
    return JobCancelResponse(
        public_id=job.public_id, status=job.status, cancel_requested=bool(job.cancel_requested)
    )


# --------------------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------------------


@router.get("/analytics/summary", response_model=AnalyticsSummary, summary="Dashboard summary")
async def analytics_summary(service: ServiceDep, principal: ReaderDep) -> AnalyticsSummary:
    """Counts across every module, plus recent audit activity."""
    return AnalyticsSummary(**await service.summary())


@router.get("/analytics/export", summary="Export analytics data")
async def analytics_export(
    service: ServiceDep,
    principal: AdminDep,
    dataset: Annotated[str, Query(pattern=f"^({'|'.join(EXPORT_DATASETS)})$")] = "jobs",
    export_format: Annotated[str, Query(alias="format", pattern="^(csv|json)$")] = "csv",
) -> Response:
    """Export jobs or campaigns as CSV or JSON."""
    content, media_type = await service.export(dataset, export_format)
    filename = f"nexus-{dataset}.{export_format}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Max-Rows": str(MAX_EXPORT_ROWS),
        },
    )

"""API schemas for the Task Manager and Analytics."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from app.schemas.common import ApiModel, PageMeta


class JobRead(ApiModel):
    """One background job and its progress."""

    public_id: str
    job_type: str
    status: str = Field(description="queued | running | completed | failed | cancelled")
    progress_current: int
    progress_total: int
    progress_percent: float
    attempts: int
    duration_ms: int | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    cancel_requested: bool
    idempotency_key: str | None = Field(
        default=None,
        description="When set, a retried request reuses this job instead of creating a second one.",
    )


class JobLogRead(ApiModel):
    """One line of a job's log."""

    level: str
    message: str
    created_at: datetime


class JobDetail(JobRead):
    """A job with its log lines and (redacted) payload."""

    logs: list[JobLogRead] = Field(default_factory=list)
    payload: dict[str, Any] | None = Field(
        default=None, description="Job arguments, with secrets already redacted."
    )


class JobListResponse(ApiModel):
    items: list[JobRead]
    meta: PageMeta


class JobCancelResponse(ApiModel):
    """Result of a cancellation request."""

    public_id: str
    status: str
    cancel_requested: bool


class ActivityItem(ApiModel):
    """One entry of recent audit activity."""

    action: str
    entity_type: str | None
    entity_id: str | None
    outcome: str
    created_at: datetime | None


class AnalyticsSummary(ApiModel):
    """Dashboard counts across every module."""

    jobs: dict[str, int] = Field(description="Job count per status.")
    proxies: dict[str, int] = Field(description="Proxy count per status.")
    inboxes: dict[str, int] = Field(description="Inbox count per status.")
    campaigns: dict[str, int] = Field(description="Campaign count per status.")
    total_jobs: int
    total_proxies: int
    total_inboxes: int
    total_campaigns: int
    active_inboxes: int
    healthy_proxies: int
    running_campaigns: int
    running_jobs: int
    completed_jobs: int
    failed_jobs: int
    recent_activity: list[ActivityItem] = Field(default_factory=list)


class ExportRequest(ApiModel):
    """Query parameters for the analytics export (documented here, read by the router)."""

    dataset: Annotated[str, Field(description="jobs | campaigns")] = "jobs"
    format: Annotated[str, Field(description="csv | json")] = "csv"

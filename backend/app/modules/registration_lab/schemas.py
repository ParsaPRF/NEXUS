"""API schemas for the Registration Lab."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.common import ApiModel, PageMeta

TargetKindLiteral = Literal["demo", "api", "ui"]


class TargetCreate(ApiModel):
    """Register a registration target.

    ``is_authorized`` defaults to **false** and ``authorization_note`` is required to use it:
    the two gates exist so a campaign cannot be created by accident against a system nobody
    authorized.
    """

    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=8, max_length=512)
    kind: TargetKindLiteral = Field(default="demo")
    adapter_name: str = Field(default="demo_api", max_length=64)
    is_authorized: bool = Field(default=False)
    authorization_note: str | None = Field(
        default=None,
        max_length=2000,
        description="Who authorized testing this target, and when. Required to run a campaign.",
    )

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")


class TargetRead(ApiModel):
    id: int
    name: str
    base_url: str
    kind: str
    adapter_name: str
    is_authorized: bool
    authorization_note: str | None
    is_active: bool
    may_be_tested: bool = Field(
        description="True only when active, authorized, documented and allowed by policy."
    )
    created_at: datetime


class CampaignCreate(ApiModel):
    """Create a campaign.

    Either ``count`` generated identities or ``identities_text`` (CSV / one address per line).
    Imported addresses must live on a domain this instance hosts, or their verification email
    could not be read.
    """

    name: str = Field(min_length=1, max_length=200)
    target_id: int
    count: Annotated[int, Field(ge=0, le=500)] = 0
    identities_text: str | None = Field(
        default=None,
        max_length=200_000,
        description="CSV or one address per line. Header row optional.",
    )
    concurrency: Annotated[int, Field(ge=1, le=5)] = 1
    rate_limit_per_minute: Annotated[int, Field(ge=1, le=120)] = 20
    email_timeout_seconds: Annotated[int, Field(ge=5, le=300)] = 30

    @field_validator("count")
    @classmethod
    def require_something(cls, value: int) -> int:
        return value


class CampaignRead(ApiModel):
    public_id: str
    name: str
    target_id: int
    status: str
    total_tests: int
    completed_tests: int
    succeeded_tests: int
    failed_tests: int
    progress_percent: float
    concurrency: int
    rate_limit_per_minute: int
    started_at: datetime | None
    finished_at: datetime | None
    stop_reason: str | None
    created_at: datetime


class CampaignCreateResponse(ApiModel):
    """The campaign plus the background job that will run it."""

    campaign: CampaignRead
    job_id: str


class CampaignListResponse(ApiModel):
    items: list[CampaignRead]
    meta: PageMeta


class RegistrationResultRead(ApiModel):
    """One analysed test outcome."""

    test_id: int
    email: str
    status: str
    outcome: str | None
    verification_code: str | None
    code_extraction_confidence: float | None
    account_status: str | None
    duration_ms: int | None
    error_message: str | None
    steps: dict[str, object] | None


class CampaignResultsResponse(ApiModel):
    campaign: CampaignRead
    items: list[RegistrationResultRead]
    meta: PageMeta
    summary: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Counts per outcome. Product defects and controls encountered are listed separately "
            "because they mean different things: one is a bug, the other a boundary."
        ),
    )

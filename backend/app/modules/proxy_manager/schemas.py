"""API schemas for the Proxy Manager.

The rule that shapes this file: **``password_encrypted`` never appears here.** The ORM model
holds it; the API contract has no field for it, so even a careless ``model_validate`` cannot
serialize it. ``username`` is exposed because it is not a secret and is needed to tell two
accounts on one endpoint apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.models.enums import ProxyStatus
from app.schemas.common import ApiModel, Page, PageMeta

#: Format accepted by the import endpoints.
ImportFormat = Literal["auto", "txt", "csv"]


class ProxyRead(ApiModel):
    """One stored proxy, as exposed by the API."""

    id: int
    source_id: int | None = Field(
        default=None, description="Import batch this proxy came from (null when added manually)."
    )
    protocol: str = Field(description="http | https | socks5")
    host: str
    port: int
    username: str | None = Field(
        default=None, description="Proxy account name, when the endpoint needs one."
    )
    status: str = Field(description="unknown | healthy | unhealthy | expired")
    latency_ms: int | None = Field(default=None, description="Last successful round-trip, in ms.")
    reliability: float | None = Field(
        default=None, description="success / (success + failure), or null when never checked."
    )
    success_count: int
    failure_count: int
    consecutive_failures: int
    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None = Field(
        default=None, description="Redacted error text from the last failed check."
    )
    is_enabled: bool
    created_at: datetime


class ProxyListResponse(ApiModel):
    """A page of proxies."""

    items: list[ProxyRead]
    meta: PageMeta


class ProxyImportRequest(ApiModel):
    """Import a proxy list from text."""

    content: str = Field(
        min_length=1, description="The list itself: newline-delimited, CSV, or a single entry."
    )
    format: ImportFormat = Field(
        default="auto", description="auto detects from the content; or force txt / csv."
    )
    name: str = Field(
        default="", max_length=128, description="Label for this import batch (source name)."
    )
    kind: Literal["manual", "file", "url"] = Field(
        default="manual",
        description="Where the list came from. 'url' only records attribution; nothing is fetched.",
    )
    location: str | None = Field(
        default=None,
        max_length=512,
        description="Optional origin note: a file path, or the URL the list was downloaded from.",
    )

    @field_validator("name")
    @classmethod
    def default_name(cls, value: str) -> str:
        """Give an unnamed import a usable label instead of an empty string."""
        return value.strip() or "Untitled import"


class RejectedEntryRead(ApiModel):
    """One rejected entry, safe to show the user."""

    line_number: int
    reason: str
    excerpt: str = Field(description="Sanitized fragment; credentials are redacted.")


class ProxyImportResponse(ApiModel):
    """What an import did."""

    source_id: int
    source_name: str
    format_detected: str
    entries_seen: int = Field(description="Non-comment entries the parser looked at.")
    accepted: int = Field(description="Entries stored as new proxies.")
    duplicates: int = Field(description="Entries skipped because they were already stored.")
    rejected: int = Field(description="Entries that could not be parsed.")
    rejected_samples: list[RejectedEntryRead] = Field(
        default_factory=list, description="First few rejections, with line numbers."
    )
    credentials_refused: int = Field(
        default=0,
        description=(
            "Entries with a password that were refused because no credential key is configured. "
            "NEXUS never stores a proxy password in plain text."
        ),
    )


class ProxyCheckResponse(ApiModel):
    """Result of checking one proxy."""

    proxy_id: int
    ok: bool
    latency_ms: int | None = None
    http_status: int | None = None
    error_class: str | None = Field(
        default=None,
        description="timeout | connect_error | proxy_error | tls_error | http_error | unknown",
    )
    error_message: str | None = None
    status: str = Field(description="Resulting proxy status: healthy | unhealthy | expired.")


class ProxyBatchCheckRequest(ApiModel):
    """Request a batch health check.

    Either an explicit list of ids, or a filter selecting proxies by status. An empty request is
    rejected: an unbounded check of the whole table is exactly the runaway fan-out the project
    refuses (threat T12).
    """

    proxy_ids: Annotated[list[int], Field(default_factory=list, max_length=500)] = Field(
        default_factory=list, description="Specific proxies to check."
    )
    status_filter: ProxyStatus | None = Field(
        default=None, description="Alternatively, check every proxy with this status."
    )
    limit: Annotated[int, Field(default=100, ge=1, le=500)] = 100

    @field_validator("proxy_ids")
    @classmethod
    def dedupe_ids(cls, value: list[int]) -> list[int]:
        """Reject duplicates in the requested id list."""
        if len(set(value)) != len(value):
            msg = "proxy_ids contains duplicates."
            raise ValueError(msg)
        return value


class ProxyBatchCheckResponse(ApiModel):
    """A queued batch check."""

    job_id: str
    job_type: str
    proxy_count: int = Field(description="How many proxies the job will check.")


class ProxyUpdateRequest(ApiModel):
    """Enable or disable a proxy without deleting its history."""

    is_enabled: bool = Field(
        description="False keeps the row and its check history but excludes it from checks."
    )


class ProxyStatsResponse(ApiModel):
    """Counts for the dashboard widgets."""

    total: int
    by_status: dict[str, int] = Field(description="Count per proxy status.")
    healthy_with_latency: int = Field(
        description="Healthy proxies with a measured latency, i.e. usable right now."
    )
    average_latency_ms: float | None = None


#: Columns exported to CSV/JSON. Deliberately excludes anything credential-related.
EXPORT_FIELDS: tuple[str, ...] = (
    "protocol",
    "host",
    "port",
    "username",
    "status",
    "latency_ms",
    "reliability",
    "success_count",
    "failure_count",
    "last_checked_at",
    "is_enabled",
    "source_id",
)


def proxy_to_export_row(proxy: ProxyRead) -> dict[str, object]:
    """Flatten a proxy into the export shape (one CSV row / one JSON object)."""
    return {
        "protocol": proxy.protocol,
        "host": proxy.host,
        "port": proxy.port,
        "username": proxy.username or "",
        "status": proxy.status,
        "latency_ms": proxy.latency_ms if proxy.latency_ms is not None else "",
        "reliability": round(proxy.reliability, 4) if proxy.reliability is not None else "",
        "success_count": proxy.success_count,
        "failure_count": proxy.failure_count,
        "last_checked_at": proxy.last_checked_at.isoformat() if proxy.last_checked_at else "",
        "is_enabled": proxy.is_enabled,
        "source_id": proxy.source_id if proxy.source_id is not None else "",
    }


#: Page of proxies, expressed with the shared generic so the UI pages every list the same way.
PageOfProxies = Page[ProxyRead]

__all__ = [
    "EXPORT_FIELDS",
    "ImportFormat",
    "PageOfProxies",
    "ProxyBatchCheckRequest",
    "ProxyBatchCheckResponse",
    "ProxyCheckResponse",
    "ProxyImportRequest",
    "ProxyImportResponse",
    "ProxyListResponse",
    "ProxyRead",
    "ProxyStatsResponse",
    "ProxyUpdateRequest",
    "RejectedEntryRead",
    "proxy_to_export_row",
]

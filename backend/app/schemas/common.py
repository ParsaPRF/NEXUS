"""Schemas shared by every module: errors, health, pagination."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiModel(BaseModel):
    """Base for API models.

    ``from_attributes`` lets a schema be built straight from an ORM object
    (``Schema.model_validate(row)``), which removes a hand-written conversion function per
    entity. ``extra="forbid"`` on requests rejects typos instead of ignoring them.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ErrorResponse(ApiModel):
    """The single error body shape produced by every failure."""

    code: str = Field(description="Stable machine-readable error code, e.g. 'ssrf_blocked'.")
    message: str = Field(description="Human-readable description. Never contains secrets.")
    details: dict[str, object] | None = Field(
        default=None, description="Optional structured context, e.g. the offending host."
    )
    request_id: str | None = Field(
        default=None, description="Correlates this response with the server logs and audit trail."
    )


class ServiceState(StrEnum):
    """Result of one readiness probe."""

    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class HealthResponse(ApiModel):
    """Liveness: the process is up and serving requests."""

    status: str = Field(default="ok")
    app: str = Field(description="Application name.")
    version: str = Field(description="Application version.")
    environment: str = Field(description="development | staging | production")
    profile: str = Field(description="local | docker - which infrastructure profile is active.")
    time: datetime = Field(description="Server time, UTC.")


class ReadinessCheck(ApiModel):
    """One dependency's readiness result."""

    name: str
    state: ServiceState
    detail: str | None = None


class ReadinessResponse(ApiModel):
    """Readiness: the process can actually do its job.

    Deliberately reports each dependency separately. "Not ready" alone is useless when you are
    trying to work out whether the database, the mail backend or the queue is the problem.
    """

    ready: bool
    checks: list[ReadinessCheck]


class PageMeta(ApiModel):
    """Pagination metadata."""

    total: int = Field(ge=0, description="Total rows matching the filter, ignoring pagination.")
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)

    @property
    def has_more(self) -> bool:
        """True when further pages exist."""
        return self.offset + self.limit < self.total


# The classic ``Generic[T]`` form is kept deliberately rather than the PEP 695 ``class Page[T]``
# syntax: the explicit form is unambiguous for readers meeting generics for the first time, and
# it keeps the class compatible with type-checkers that have not adopted PEP 695 inference.
class Page(ApiModel, Generic[T]):  # noqa: UP046
    """A paginated list response.

    Every list endpoint returns this shape so the desktop UI can implement paging once and
    reuse it everywhere. Unbounded list endpoints are refused by design (threat T7).
    """

    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], *, total: int, limit: int, offset: int) -> Page[T]:
        """Assemble a page from already-fetched items and a total count."""
        return cls(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))

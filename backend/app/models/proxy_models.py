"""Module A - Proxy Manager models.

These tables hold network proxies that the **user owns, operates, or is authorized to use**,
plus the history of health checks against a configured test endpoint.

Two design points worth understanding:

* **Source attribution is a security control, not bookkeeping.** Every imported proxy points at
  the :class:`ProxySource` it came from, including accepted/rejected counts, so an untrusted
  list can always be traced and revoked as a unit.
* **Current state and history are separate tables.** ``proxies.status`` answers "can I use this
  now"; ``proxy_checks`` answers "has it been reliable". A single counter cannot answer both,
  and the reliability ratio users actually want needs the history.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import ProxyProtocol, ProxySourceKind, ProxyStatus

if TYPE_CHECKING:
    from app.models.job_models import Campaign


class ProxySource(Base, TimestampMixin):
    """A batch of proxies that arrived together, from one place."""

    __tablename__ = "proxy_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ProxySourceKind.MANUAL.value,
        doc="manual | file | url",
    )
    location: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        doc="File path or URL. A 'file' source is never fetched over the network.",
    )
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="Raw lines/rows seen in the import."
    )
    accepted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="Entries that parsed into a usable proxy."
    )
    rejected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="Malformed or duplicate entries."
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    proxies: Mapped[list[Proxy]] = relationship(
        back_populates="source",
        cascade="save-update, merge",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<ProxySource id={self.id} name={self.name!r} kind={self.kind!r}>"


class Proxy(Base, TimestampMixin):
    """One proxy endpoint and its most recent known health."""

    __tablename__ = "proxies"
    __table_args__ = (
        # Functional de-duplication: the same endpoint with the same account is one proxy.
        # ``username`` participates because two accounts on one host are genuinely different
        # proxies with different quotas. NULL usernames do not collide in SQL (NULLs are
        # distinct in a UNIQUE index), so the importer ALSO de-duplicates in application code
        # using a normalized key that treats NULL as an empty string.
        UniqueConstraint(
            "protocol", "host", "port", "username", name="uq_proxies_endpoint_identity"
        ),
        # Dashboard query: "healthy proxies, fastest first".
        Index("ix_proxies_status_latency", "status", "latency_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxy_sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    protocol: Mapped[str] = mapped_column(
        String(8), nullable=False, default=ProxyProtocol.HTTP.value
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        doc=(
            "Reversibly encrypted, never plain text and never hashed (a proxy password must be "
            "replayed). If no credential key is configured, credentials are refused at import "
            "time instead of being stored."
        ),
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProxyStatus.UNKNOWN.value, index=True
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, doc="Last successful round-trip in milliseconds."
    )
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        doc="Used by the expiry policy: N failures in a row suggests an expired subscription.",
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(
        String(512), nullable=True, doc="Redacted error text from the last failed check."
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, doc="Soft disable without deleting history."
    )

    source: Mapped[ProxySource | None] = relationship(back_populates="proxies")
    checks: Mapped[list[ProxyCheck]] = relationship(
        back_populates="proxy",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # -- Derived helpers ---------------------------------------------------------------
    # NOTE: there is deliberately NO relationship between Proxy and Campaign. Registration
    # must not be able to use the proxy pool (Decision D8); the absence of the link is the
    # enforcement, so do not add one.

    @property
    def reliability(self) -> float | None:
        """Success ratio in ``0.0..1.0``, or ``None`` when never checked."""
        successes = self.success_count or 0
        failures = self.failure_count or 0
        total = successes + failures
        return None if total == 0 else successes / total

    @property
    def display_address(self) -> str:
        """``scheme://host:port`` with any credentials omitted, safe for logs and exports."""
        return f"{self.protocol}://{self.host}:{self.port}"

    def __repr__(self) -> str:
        return f"<Proxy id={self.id} {self.display_address} status={self.status!r}>"


class ProxyCheck(Base):
    """One health-check attempt against one proxy.

    Kept as history so reliability is measured rather than assumed. A scheduled retention job
    (Phase 5) prunes old rows so the table stays bounded.
    """

    __tablename__ = "proxy_checks"
    __table_args__ = (Index("ix_proxy_checks_proxy_checked", "proxy_id", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_id: Mapped[int] = mapped_column(
        ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_class: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="timeout | connect_refused | tls_error | dns_failure | http_error | unknown",
    )
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)

    proxy: Mapped[Proxy] = relationship(back_populates="checks")

    def __repr__(self) -> str:
        return f"<ProxyCheck proxy_id={self.proxy_id} ok={self.ok} latency_ms={self.latency_ms}>"


# ``Campaign`` is imported only for type checking to avoid a circular import at runtime.
__all__ = ["Campaign", "Proxy", "ProxyCheck", "ProxySource"]

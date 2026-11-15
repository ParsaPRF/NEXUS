"""Module B - Email Manager models.

Test inboxes live on a domain the project owner controls, backed by Mailpit. The
``email_domains`` table is where that rule is enforced in data: a domain must be RFC-reserved
(``example.test`` and friends, reserved by RFC 2606/6761 precisely for testing) or explicitly
marked as owner-verified. There is no code path that creates a mailbox on a third-party provider
(Decision D8, docs/SECURITY.md 3).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import InboxStatus

if TYPE_CHECKING:
    from app.models.job_models import Campaign


class EmailDomain(Base, TimestampMixin):
    """A domain that test inboxes may be created on."""

    __tablename__ = "email_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    is_reserved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc=(
            "True for RFC 2606/6761 reserved test domains. Kept as our default posture: a "
            "reserved domain cannot receive real mail, so it cannot become a spam channel."
        ),
    )
    owner_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="Set only when the operator confirms they control the domain (a real test domain).",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    inboxes: Mapped[list[Inbox]] = relationship(back_populates="domain", passive_deletes=True)

    @property
    def is_usable(self) -> bool:
        """A domain may host test inboxes only if it is active *and* safely attributable."""
        # ``is_active`` / ``is_reserved`` may be None on an object built in memory but not yet
        # flushed (SQLAlchemy applies defaults at INSERT time). Treat None as "not allowed", so
        # a half-built object fails closed rather than raising.
        if self.is_active is not True:
            return False
        return bool(self.is_reserved or self.owner_verified)

    def __repr__(self) -> str:
        return f"<EmailDomain id={self.id} domain={self.domain!r} reserved={self.is_reserved}>"


class Inbox(Base, TimestampMixin):
    """A disposable test inbox address."""

    __tablename__ = "inboxes"
    __table_args__ = (
        # The same alias must not exist twice on one domain. ``address`` is also unique, but
        # keeping both constraints means a lookup by either form is indexed.
        UniqueConstraint("domain_id", "local_part", name="uq_inboxes_domain_local_part"),
        # Expiry sweeper: "active inboxes whose expires_at has passed".
        Index("ix_inboxes_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, index=True, doc="UUID exposed by the API."
    )
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("email_domains.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    local_part: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        unique=True,
        index=True,
        doc="local_part@domain, denormalized so SMTP routing is a single indexed lookup.",
    )
    label: Mapped[str | None] = mapped_column(
        String(128), nullable=True, doc="Human note, e.g. campaign-42 or manual-qa."
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=InboxStatus.ACTIVE.value, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, doc="Counter column to avoid COUNT(*) on hot paths."
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    domain: Mapped[EmailDomain] = relationship(back_populates="inboxes")
    messages: Mapped[list[Message]] = relationship(
        back_populates="inbox",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    campaign: Mapped[Campaign | None] = relationship(back_populates="inboxes")

    def __repr__(self) -> str:
        return f"<Inbox id={self.id} address={self.address!r} status={self.status!r}>"


class Message(Base, TimestampMixin):
    """A message received by a test inbox."""

    __tablename__ = "messages"
    __table_args__ = (
        # Idempotent ingestion: polling the mail backend twice cannot create duplicates.
        UniqueConstraint("inbox_id", "backend_message_id", name="uq_messages_inbox_backend_id"),
        # Newest-first listing for one inbox.
        Index("ix_messages_inbox_received", "inbox_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inbox_id: Mapped[int] = mapped_column(
        ForeignKey("inboxes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    backend_message_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True, doc="Mailpit's identifier, used for reconciliation."
    )
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc=(
            "Stored for completeness but NEVER rendered by the UI: an email body is untrusted "
            "input and rendering HTML from it invites script injection (threat T10)."
        ),
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, doc="Cached result of verification-code extraction."
    )
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    headers_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, doc="Selected headers only, size-capped before storage."
    )

    inbox: Mapped[Inbox] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message id={self.id} inbox_id={self.inbox_id} subject={self.subject!r}>"

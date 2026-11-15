"""Models for local users and the append-only audit trail."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import AuditOutcome


class User(Base, TimestampMixin):
    """A local NEXUS user.

    Passwords are stored only as Argon2id hashes (docs/SECURITY.md T5). The ``role`` column
    exists now so that introducing real multi-user support later is a configuration change
    rather than a migration plus a refactor.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc="Argon2id hash. There is no code path that stores a plaintext password.",
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    audit_logs: Mapped[list[AuditLog]] = relationship(
        back_populates="user", cascade="save-update, merge", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role!r}>"


class AuditLog(Base):
    """An append-only record of a security-relevant action.

    Convention: rows are inserted and never updated or deleted, which is why this model does not
    use :class:`~app.database.base.TimestampMixin`. There is deliberately no update or delete
    code path anywhere in the application.

    ``detail_json`` is redacted before it is written (docs/SECURITY.md T3): an audit trail that
    leaks the secret it was recording is worse than no audit trail.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # "Show me everything that happened to this entity."
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        # Retention and "what happened today" queries.
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, doc="e.g. proxy.import, target.authorize"
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AuditOutcome.SUCCESS.value
    )
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # NOTE: no `index=True` here. The index is declared once, with an explicit name, in
    # __table_args__ below. Declaring both would make SQLAlchemy emit two indexes for the same
    # column under the same name, which fails at CREATE TABLE time.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User | None] = relationship(back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog action={self.action!r} outcome={self.outcome!r}>"

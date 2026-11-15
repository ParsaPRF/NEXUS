"""Declarative base and shared column mixins.

The metadata object created here is the single source of truth that Alembic compares against
when autogenerating migrations, so **every model must be imported before a migration runs**.
That import happens in :mod:`app.models`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Explicit naming convention for constraints and indexes.
#:
#: Without this, SQLite and PostgreSQL invent different names for the same constraint, and
#: Alembic cannot reliably drop or alter them later. Setting it once here avoids a whole class
#: of "works on SQLite, fails on Postgres" migration bugs.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` to a model.

    ``server_default`` means the *database* fills the value, so rows created by raw SQL or a
    migration script are also stamped. ``onupdate`` is applied by SQLAlchemy on ORM updates.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="Row creation time (UTC).",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
        doc="Last modification time (UTC).",
    )

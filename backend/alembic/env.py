"""Alembic migration environment.

This is the glue Alembic calls for every migration command. Three choices worth understanding:

1. **The URL comes from the environment, not from ``alembic.ini``.** ``DATABASE_URL`` is read
   directly, and only if it is absent do we fall back to the application's settings. A committed
   URL means a committed password, so the ini file stays secret-free.

2. **Migrations run synchronously.** Alembic's command API is synchronous. We therefore convert
   the application's async URL to a sync one (``sqlite+aiosqlite`` -> ``sqlite``,
   ``postgresql+asyncpg`` -> ``postgresql+psycopg``) instead of dragging an async engine through
   the migration path.

3. **``app.models`` is imported so ``target_metadata`` is complete.** A model that is never
   imported is missing from the metadata, and autogenerate would then treat its table as
   unnecessary - potentially emitting a ``DROP TABLE``. Importing the package is what makes
   ``alembic revision --autogenerate`` safe.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the `app` package importable when Alembic is invoked from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  (side effect: registers every table in Base.metadata)
from app.database.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Every table NEXUS knows about. Autogenerate diffs the live database against this.
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the synchronous database URL used for migrations.

    Order of precedence:
      1. ``DATABASE_URL`` - explicit, set by Docker Compose or CI.
      2. ``NEXUS_DATABASE__URL`` - the application's own setting, converted to a sync driver.
      3. The local SQLite default, so a fresh clone can migrate with nothing configured.
    """
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    configured = os.environ.get("NEXUS_DATABASE__URL")
    if configured:
        return _to_sync_url(configured)

    try:
        from app.core.config import get_settings

        return get_settings().database.sync_url
    except Exception:  # pragma: no cover - settings unavailable, fall back to the default
        return "sqlite:///./nexus.db"


def _to_sync_url(url: str) -> str:
    """Strip the async driver suffix from a SQLAlchemy URL."""
    for async_driver, sync_driver in (("+aiosqlite", ""), ("+asyncpg", "+psycopg")):
        if async_driver in url:
            return url.replace(async_driver, sync_driver, 1)
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``).

    Useful for reviewing exactly what a migration will do before it touches a real database.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # one short-lived connection: no pooling in a CLI tool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detect column type and default changes, not just added/removed tables. Without
            # these, a changed column type would silently produce no revision at all.
            compare_type=True,
            compare_server_default=True,
            # SQLite cannot ALTER most things; batch mode rewrites the table via a copy.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

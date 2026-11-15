"""Async database engine, session factory and FastAPI session dependency.

Why async?
----------
The backend is I/O-bound (Decision D4). An async engine lets one process handle many
simultaneous database operations without a thread per request.

Why SQLite works here
---------------------
SQLite is the default *local* profile so the project runs with no server (Decision D3). It is
a real database with real transactions; the only concessions are:

* ``StaticPool`` + ``check_same_thread=False`` for ``:memory:`` databases, because an
  in-memory database disappears when its last connection closes. A single shared connection
  keeps it alive for the duration of a test.
* No pool-size arguments, which SQLite's pool does not accept.

PostgreSQL (docker profile) uses a normal sized pool. Both are produced by the same function,
so there is exactly one place where dialect differences live.

The session is deliberately **not** committed automatically. Services own their transaction
boundaries: a service method either completes and commits, or raises and the whole unit of work
is rolled back. Auto-commit hides bugs and makes retries unsafe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import Settings

#: Set by ``init_engine``; ``None`` until the application (or a test) starts up.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    """Build an :class:`AsyncEngine` from settings, handling SQLite specifics."""
    url = settings.database.url
    kwargs: dict[str, object] = {
        "echo": settings.database.echo,
        "future": True,
        "pool_pre_ping": True,  # transparently recovers a dropped connection
    }

    if settings.database.is_sqlite:
        # SQLite does not support the server-side pool tuning parameters.
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = settings.database.pool_size
        kwargs["max_overflow"] = settings.database.max_overflow

    engine = create_async_engine(url, **kwargs)

    if settings.database.is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: object, _record: object) -> None:
            """SQLite ignores foreign keys unless asked; without this, ON DELETE rules do nothing.

            This matters: our schema relies on ``ON DELETE CASCADE`` for campaigns and messages.
            """
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_engine(settings: Settings) -> AsyncEngine:
    """Create and register the process-wide engine and session factory.

    Called from the FastAPI lifespan handler and from test fixtures.
    """
    global _engine, _session_factory  # noqa: PLW0603 - module-level singleton by design
    if _engine is not None:
        return _engine
    _engine = create_engine(settings)
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        # Attributes stay readable after commit, so a service can return a committed object
        # without triggering a lazy load outside the session.
        expire_on_commit=False,
        autoflush=False,
    )
    return _engine


async def dispose_engine() -> None:
    """Close the pool and forget the engine. Used at shutdown and between tests."""
    global _engine, _session_factory  # noqa: PLW0603 - module-level singleton by design
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    """Return the active engine.

    Raises:
        RuntimeError: the engine was never initialised - a programming error, not user input.
    """
    if _engine is None:
        msg = "Database engine is not initialised. Call init_engine() during application startup."
        raise RuntimeError(msg)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the active session factory."""
    if _session_factory is None:
        msg = "Session factory is not initialised. Call init_engine() during application startup."
        raise RuntimeError(msg)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that is rolled back on error.

    On success the session is closed without an implicit commit: a route that forgot to commit
    produces a no-op, which is easy to spot in tests, instead of a half-written change.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


#: Convenient alias so routes read as ``session: DbSession``.
DbSession = Annotated[AsyncSession, Depends(get_session)]

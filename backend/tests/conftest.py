"""Shared pytest fixtures.

Two decisions shape this file:

**1. Tests never touch real infrastructure.**
The database is in-memory SQLite and the job queue runs in-process, so the suite is fast,
hermetic and runnable on a machine with no Docker daemon (docs/DECISION_LOG.md D3, D9). Tests
that genuinely need PostgreSQL, Redis or Mailpit are marked ``e2e`` and skipped by default.

**2. The application is built per test through ``create_app(settings)``.**
``app.state.settings`` is what the routes, the lifespan and the middleware read, so a freshly
built app is completely isolated. Nothing here mutates the process-wide settings singleton,
which means test order cannot change a result.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

# --------------------------------------------------------------------------------------
# Environment must be set before `app.core.config` is imported anywhere: settings are
# validated at first use, and `app.main` builds an application at import time.
# --------------------------------------------------------------------------------------
os.environ.setdefault("NEXUS_SECRET_KEY", "test-secret-key-that-is-long-enough-0123456789")
os.environ.setdefault("NEXUS_API_KEY", "test-api-key-0123456789")
os.environ.setdefault("NEXUS_PROXY_CREDENTIAL_KEY", "test-credential-key-0123456789")
os.environ.setdefault("NEXUS_LOG_LEVEL", "WARNING")
os.environ.setdefault("NEXUS_DATABASE__URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("NEXUS_JOBS__BACKEND", "inprocess")
os.environ.setdefault("NEXUS_MAIL__BACKEND", "memory")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

# --------------------------------------------------------------------------------------
# Synthetic job handlers, registered once for the whole session.
#
# They live here because more than one test module needs them (task manager and RQ backend), and
# `register_handler` refuses to register the same job type twice with different functions. A
# per-module registration would make the tests order-dependent.
# --------------------------------------------------------------------------------------
from app.core.exceptions import NotFoundError  # noqa: E402
from app.jobs.queue import get_handler  # noqa: E402
from app.modules.task_manager.tracker import register_tracked_handler  # noqa: E402


def _register_once(job_type: str, handler) -> None:  # type: ignore[no-untyped-def]
    """Register a synthetic handler unless it already exists.

    ``register_handler`` refuses a second registration with a different function object. pytest can
    import this conftest under two module names (``conftest`` and ``tests.conftest``) depending on
    how a test module is collected, which would otherwise raise during collection.
    """
    try:
        get_handler(job_type)
    except NotFoundError:
        register_tracked_handler(job_type, handler)


async def _test_slow_job(context, payload):  # type: ignore[no-untyped-def]
    """A cancellable job: checks for cancellation between steps."""
    import asyncio

    context.set_total(100)
    for index in range(100):
        context.check_cancelled()
        context.advance(message=f"step {index}")
        await asyncio.sleep(0.02)
    return {"steps": 100}


async def _test_progress_job(context, payload):  # type: ignore[no-untyped-def]
    """A short job that reports progress and log messages."""
    import asyncio

    context.set_total(3)
    for index in range(3):
        context.advance(message=f"processed {index + 1}")
        await asyncio.sleep(0.01)
    return {"processed": 3}


_register_once("test.slow", _test_slow_job)
_register_once("test.progress", _test_progress_job)

from app.core.config import Settings, get_settings  # noqa: E402
from app.database.engine import (  # noqa: E402
    dispose_engine,
    get_session_factory,
    init_engine,
)
from app.jobs.queue import reset_queue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402

#: The API key the test client sends. Matches NEXUS_API_KEY above.
TEST_API_KEY = "test-api-key-0123456789"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Force pytest-asyncio/anyio integration onto asyncio, not trio."""
    return "asyncio"


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    """Isolated settings for a single test.

    The SQLite URL points at a file inside pytest's per-test temporary directory rather than
    ``:memory:``. Both work, but a real file makes the schema visible to Alembic and to a
    debugging session, and it avoids any chance of state leaking between tests.

    ``_env_file=None`` stops pydantic-settings from reading a developer's local ``.env``: a test
    must not depend on - or be broken by - whatever happens to be in that file.
    """
    return Settings(
        _env_file=None,
        profile="local",
        environment="development",
        log_level="WARNING",
        log_format="console",
        log_directory=tmp_path / "logs",
        api_key=TEST_API_KEY,
        secret_key="test-secret-key-that-is-long-enough-0123456789",
        proxy_credential_key="test-credential-key-0123456789",
        database={"url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"},
        mail={"backend": "memory", "reserved_domain": "example.test"},
        jobs={"backend": "inprocess", "max_workers": 1},
        proxy={
            "check_url": "http://127.0.0.1:8000/health",
            "expire_after_failures": 2,
            "max_import_bytes": 65536,
            "batch_limit": 50,
        },
        network={
            "allow_private_targets": False,
            "allowed_target_hosts": [],
            "http_timeout_seconds": 5.0,
            "max_concurrent_checks": 2,
            "max_response_bytes": 65536,
        },
    )


@pytest.fixture
async def db_engine(test_settings: Settings) -> AsyncIterator[object]:
    """Create every table in the test database and tear the engine down afterwards."""
    engine = init_engine(test_settings)
    async with engine.begin() as connection:
        # Importing app.models (done in conftest) registers all tables in Base.metadata.
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await dispose_engine()


@pytest.fixture
async def session(test_settings: Settings, db_engine: object) -> AsyncIterator[object]:
    """An ``AsyncSession`` bound to the test database."""
    factory = get_session_factory()
    async with factory() as db_session:
        yield db_session
        await db_session.rollback()


@pytest.fixture
def app(test_settings: Settings):
    """A NEXUS application built with isolated settings."""
    return create_app(test_settings)


@pytest.fixture
async def client(app, db_engine: object) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the application through ASGI.

    ``ASGITransport`` drives the app in-process: no socket, no port, no timing flakiness. The
    lifespan is not run (ASGITransport does not do that), which is why ``db_engine`` is a
    dependency here - it is what creates the tables.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://nexus.test",
        headers={"X-API-Key": TEST_API_KEY},
    ) as http_client:
        yield http_client


@pytest.fixture
async def anonymous_client(app, db_engine: object) -> AsyncIterator[AsyncClient]:
    """A client that sends **no** API key, for authentication tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://nexus.test") as http_client:
        yield http_client


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    """Clear cached settings and the job queue around every test.

    ``get_settings`` is ``lru_cache``d and the job queue is a module-level singleton. Both are
    correct in production and both would leak state between tests if left alone.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _clean_queue() -> AsyncIterator[None]:
    """Ensure no in-process job outlives its test."""
    yield
    await reset_queue()


@pytest.fixture(autouse=True)
def _clean_mail_backend() -> Iterator[None]:
    """Reset the mail backend between tests.

    The in-memory backend is a process-wide singleton, so without this a message delivered in one
    test would be visible in the next - and a test that passes alone would fail in a suite.
    """
    from app.modules.email_manager.backends import reset_backend

    reset_backend()
    yield
    reset_backend()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``e2e`` tests unless they were explicitly requested with ``-m e2e``.

    The default run must stay green on a machine with no Docker daemon. Opting in is explicit
    so nobody mistakes a skipped end-to-end test for a passing one.
    """
    marker_expression = config.getoption("-m", default="")
    if "e2e" in marker_expression:
        return
    skip_marker = pytest.mark.skip(reason="e2e test: run with '-m e2e' against real infrastructure")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_marker)

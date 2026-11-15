"""Health and readiness endpoints.

Two different questions, two different endpoints - a distinction that matters in practice:

* ``/health`` (liveness) - "is this process alive?" It touches **nothing** external and must
  always answer quickly. If it checked the database, a brief database outage would cause an
  orchestrator to kill and restart a perfectly healthy process, which makes the outage worse.
* ``/health/ready`` (readiness) - "can this process do useful work?" It checks each dependency
  and reports them individually, so a failure says *which* one is broken.

Neither endpoint requires authentication: a probe cannot present an API key, and they expose no
data beyond a version string and per-dependency state.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.engine import get_engine, get_session_factory
from app.schemas.common import (
    HealthResponse,
    ReadinessCheck,
    ReadinessResponse,
    ServiceState,
)

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    """Report that the process is running.

    Intentionally dependency-free: it must not fail because a downstream service is down.
    """
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=__version__,
        environment=settings.environment,
        profile=settings.profile.value,
        time=datetime.now(UTC),
    )


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(settings: SettingsDep) -> ReadinessResponse:
    """Check each dependency and report the result individually.

    Returns HTTP 200 even when not ready. The body carries the detail, and a non-200 would make
    the response indistinguishable from "the endpoint itself is broken" for many clients.
    """
    checks: list[ReadinessCheck] = [
        await _check_database(),
        _check_mail_backend(settings),
        _check_job_backend(settings),
        _check_security_configuration(settings),
    ]

    ready = all(check.state is not ServiceState.FAILED for check in checks)
    return ReadinessResponse(ready=ready, checks=checks)


async def _check_database() -> ReadinessCheck:
    """Run ``SELECT 1`` through the real engine and session factory."""
    started = time.perf_counter()
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except RuntimeError as exc:
        # Engine was never initialised - a startup problem, not a transient failure.
        return ReadinessCheck(name="database", state=ServiceState.FAILED, detail=str(exc))
    except SQLAlchemyError as exc:
        logger.warning("Database readiness check failed: %s", exc)
        return ReadinessCheck(
            name="database",
            state=ServiceState.FAILED,
            detail=f"{type(exc).__name__}: {exc}"[:300],
        )
    except OSError as exc:
        return ReadinessCheck(
            name="database", state=ServiceState.FAILED, detail=f"connection error: {exc}"[:300]
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return ReadinessCheck(name="database", state=ServiceState.OK, detail=f"{elapsed_ms}ms")


def _check_mail_backend(settings: Settings) -> ReadinessCheck:
    """Report the configured mail backend without contacting it.

    An active SMTP probe would need a socket and a timeout on every readiness call, which is a
    lot of cost for a component that is allowed to be offline while the API still serves
    requests. The configured backend name is the useful information here; actual delivery is
    verified by the Email Manager's own tests.
    """
    backend = settings.mail.backend
    if backend == "memory":
        return ReadinessCheck(
            name="mail_backend",
            state=ServiceState.DEGRADED,
            detail="in-memory backend active: messages do not leave the process (local profile)",
        )
    return ReadinessCheck(
        name="mail_backend",
        state=ServiceState.SKIPPED,
        detail=f"configured: mailpit at {settings.mail.api_url}",
    )


def _check_job_backend(settings: Settings) -> ReadinessCheck:
    """Report whether the job backend is the in-process runner or RQ."""
    if settings.jobs.backend == "inprocess":
        return ReadinessCheck(
            name="job_backend",
            state=ServiceState.OK,
            detail="in-process runner: jobs execute inside the API process",
        )
    return ReadinessCheck(
        name="job_backend",
        state=ServiceState.SKIPPED,
        detail=f"configured: rq at {settings.jobs.redis_url} (queue={settings.jobs.queue_name})",
    )


def _check_security_configuration(settings: Settings) -> ReadinessCheck:
    """Surface insecure configuration instead of hiding it.

    Running without an API key is acceptable for local development but must be *visible*, so it
    is reported as ``degraded`` rather than ``ok``. It is never ``failed``, because refusing to
    serve would be worse than serving a warning.
    """
    problems: list[str] = []
    if not settings.api_key:
        problems.append("no API key configured (unauthenticated API)")
    if settings.network.allow_private_targets:
        problems.append("private targets allowed (SSRF guard relaxed)")
    if settings.mail.backend == "memory" and settings.is_production:
        problems.append("in-memory mail backend in production")

    if problems:
        return ReadinessCheck(
            name="security", state=ServiceState.DEGRADED, detail="; ".join(problems)
        )
    return ReadinessCheck(name="security", state=ServiceState.OK, detail=None)


@router.get("/health/db", include_in_schema=False)
async def database_info(settings: SettingsDep) -> dict[str, object]:
    """Minimal, non-sensitive database fact for troubleshooting.

    Reports the dialect only. Connection strings routinely embed a password, so the URL itself
    is never returned.
    """
    engine = get_engine()
    return {
        "dialect": engine.dialect.name,
        "driver": engine.dialect.driver,
        "foreign_keys_enforced": settings.database.is_sqlite,
    }

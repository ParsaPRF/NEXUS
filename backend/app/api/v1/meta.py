"""Instance metadata endpoints.

These are the only ``/api/v1`` endpoints that exist in Phase 1. They are not filler: they are
the smallest real surface that exercises authentication, the audit trail and the error contract
end to end, which is exactly what a foundation phase should prove.

They are also a deliberate demonstration that the API returns *configuration facts*, never
secrets. ``GET /api/v1/meta`` reports whether an API key is configured; it never returns the key.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app import __version__
from app.core.config import Settings, get_settings
from app.core.security import Principal, require_admin
from app.database.engine import get_engine
from app.models.enums import AuditOutcome
from app.schemas.common import ApiModel
from app.services.audit_service import record_audit

router = APIRouter(prefix="/meta", tags=["meta"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


class InstanceInfo(ApiModel):
    """Non-sensitive description of this NEXUS instance."""

    app: str = Field(description="Application name.")
    version: str = Field(description="Application version.")
    environment: str = Field(description="development | staging | production")
    profile: str = Field(description="local | docker")
    auth_enabled: bool = Field(
        description="Whether an API key is required. Reported so the UI can warn the user."
    )
    database_dialect: str = Field(
        description="sqlite | postgresql. The URL itself is never returned."
    )
    mail_backend: str = Field(description="memory | mailpit")
    job_backend: str = Field(description="inprocess | rq")


class SafetyLimits(ApiModel):
    """The safety-relevant limits currently in force.

    Exposed so the desktop UI can display them and so an operator can confirm at a glance that
    a development escape hatch is not enabled in a shared environment.
    """

    allow_private_targets: bool = Field(
        description="When true, the SSRF guard permits private addresses (development only)."
    )
    allowed_target_hosts: list[str] = Field(
        description="Hosts registration tests may target. Empty means the bundled demo app only."
    )
    http_timeout_seconds: float
    max_concurrent_checks: int
    max_response_bytes: int
    registration_disabled_reason: str | None = Field(
        default=None,
        description="Why registration runs would currently be refused, if any.",
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> SafetyLimits:
        """Build the limits view from settings, explaining why registration may be refused."""
        reason: str | None = None
        if not settings.network.allowed_target_hosts:
            reason = (
                "No authorized target hosts are configured; only the bundled demonstration "
                "application may be used."
            )
        return cls(
            allow_private_targets=settings.network.allow_private_targets,
            allowed_target_hosts=list(settings.network.allowed_target_hosts),
            http_timeout_seconds=settings.network.http_timeout_seconds,
            max_concurrent_checks=settings.network.max_concurrent_checks,
            max_response_bytes=settings.network.max_response_bytes,
            registration_disabled_reason=reason,
        )


@router.get("", response_model=InstanceInfo, summary="Describe this instance")
async def instance_info(
    settings: SettingsDep,
    principal: Annotated[Principal, Depends(require_admin)],
) -> InstanceInfo:
    """Return non-sensitive instance facts. Requires a valid API key."""
    engine = get_engine()
    return InstanceInfo(
        app=settings.app_name,
        version=__version__,
        environment=settings.environment,
        profile=settings.profile.value,
        auth_enabled=bool(settings.api_key),
        database_dialect=engine.dialect.name,
        mail_backend=settings.mail.backend,
        job_backend=settings.jobs.backend,
    )


@router.get("/limits", response_model=SafetyLimits, summary="Show the safety limits in force")
async def safety_limits(
    settings: SettingsDep,
    principal: Annotated[Principal, Depends(require_admin)],
) -> SafetyLimits:
    """Return the active safety limits so the UI can display them."""
    return SafetyLimits.from_settings(settings)


@router.post(
    "/audit-selftest",
    status_code=202,
    summary="Write an audit record (connectivity self-test)",
)
async def audit_selftest(
    request: Request,
    settings: SettingsDep,
    principal: Annotated[Principal, Depends(require_admin)],
) -> dict[str, object]:
    """Write one audit record and read it back.

    Purpose: prove that the audit path works, that redaction is applied, and that a secret
    passed in the payload does **not** reach storage. It is a self-test, not a feature, so it
    refuses to run outside the development environment - a production audit trail should only
    ever contain real events.
    """
    from app.database.engine import get_session_factory

    if settings.is_production:
        from app.core.exceptions import PolicyViolationError

        raise PolicyViolationError("The audit self-test is disabled in production.")

    factory = get_session_factory()
    async with factory() as session:
        # The value below is intentionally shaped like a secret: the assertion is that it is
        # redacted on the way in, so it can never be read back out of the audit table.
        record = await record_audit(
            session,
            "system.audit_selftest",
            outcome=AuditOutcome.SUCCESS,
            entity_type="audit_log",
            detail={"api_key": settings.api_key or "unset", "note": "redaction self-test"},
            request=request,
            strict=True,
        )
        await session.commit()

    stored = (record.detail_json or {}) if record else {}
    return {
        "recorded": record is not None,
        "audit_id": record.id if record else None,
        "secret_was_redacted": stored.get("api_key") == "***REDACTED***",
        "request_id": getattr(request.state, "request_id", None),
    }

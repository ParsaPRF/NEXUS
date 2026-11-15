"""Audit trail service.

Every security-relevant action writes a row here: imports, checks, target authorizations,
campaign starts and stops, and policy refusals. Two properties matter:

1. **Append-only.** There is no update or delete method, deliberately. An audit trail that can
   be edited is not evidence.
2. **Redacted before persisting.** ``detail_json`` is passed through
   :func:`app.core.logging.redact`, so recording "user changed password" cannot accidentally
   store the password (docs/SECURITY.md T3).

Audit writes are best-effort by default: a failure to write the trail must not break the user's
action, but it must be logged loudly. ``strict=True`` flips that for the cases where the audit
record *is* the point (for example, authorizing a registration target).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, redact
from app.models.core_models import AuditLog
from app.models.enums import AuditOutcome

logger = get_logger(__name__)


class AuditService:
    """Writes audit records for security-relevant actions."""

    def __init__(self, session: AsyncSession, *, enabled: bool = True) -> None:
        self._session = session
        self._enabled = enabled

    async def record(
        self,
        action: str,
        *,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        user_id: int | None = None,
        entity_type: str | None = None,
        entity_id: str | int | None = None,
        detail: dict[str, Any] | None = None,
        client_ip: str | None = None,
        request_id: str | None = None,
        note: str | None = None,
        strict: bool = False,
    ) -> AuditLog | None:
        """Append one audit record.

        Args:
            action: Dotted action name, e.g. ``"proxy.import"``.
            outcome: ``success`` | ``denied`` | ``error``.
            user_id: Acting local user, when known.
            entity_type: Entity class name the action targeted.
            entity_id: Identifier of that entity.
            detail: Extra context. **Redacted automatically** - do not pre-redact, and never
                rely on this for secrets you actually need to keep.
            client_ip: Caller address.
            request_id: Correlates with application logs and the HTTP response.
            note: Free-text human note.
            strict: When true, a failure to write the record is raised instead of logged.

        Returns:
            The created record, or ``None`` when auditing is disabled.
        """
        if not self._enabled:
            return None

        record = AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            outcome=AuditOutcome(outcome).value,
            # Redact *before* the value reaches the session: redacting after a flush would be
            # too late, and doing it here means no future caller can forget.
            detail_json=redact(detail) if detail else None,
            client_ip=client_ip,
            request_id=request_id,
            note=note,
            created_at=datetime.now(UTC),
        )
        try:
            self._session.add(record)
            await self._session.flush()
        except Exception:
            await self._session.rollback()
            logger.exception(
                "Failed to write audit record", extra={"module_name": "audit", "action": action}
            )
            if strict:
                raise
            return None
        return record


async def record_audit(
    session: AsyncSession,
    action: str,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
    request: Any | None = None,
    strict: bool = False,
) -> AuditLog | None:
    """Convenience wrapper for the common "audit this from a route" case.

    Extracts the request id, client IP and authenticated principal from a FastAPI request when
    one is supplied, so routes do not repeat that boilerplate.
    """
    client_ip: str | None = None
    request_id: str | None = None
    user_id: int | None = None

    if request is not None:
        client_ip = getattr(getattr(request, "client", None), "host", None)
        request_id = getattr(request.state, "request_id", None)
        principal = getattr(request.state, "principal", None)
        # Principal is an API-key identity, not necessarily a User row; only a real user id
        # would be assigned here once local user accounts are introduced.
        user_id = getattr(principal, "user_id", None)

    return await AuditService(session).record(
        action,
        outcome=outcome,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
        client_ip=client_ip,
        request_id=request_id,
        strict=strict,
    )

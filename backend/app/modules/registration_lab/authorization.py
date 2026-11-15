"""Target authorization: the two independent gates.

Nothing may be registered against a system the owner has not authorized. Two gates enforce that,
and they fail closed:

1. **The stored record** must be active, explicitly marked ``is_authorized``, and carry a written
   ``authorization_note`` saying who authorized it. The column defaults to false precisely so a
   forgotten flag refuses the campaign.
2. **The host allowlist** (``NEXUS_NETWORK__ALLOWED_TARGET_HOSTS``) must contain the target's
   host, and the URL must pass the SSRF guard.

The bundled demonstration application is the one exception, and it is a narrow one: targets whose
``kind`` is ``demo`` are permitted without an external allowlist entry **because they must be
local**. A "demo" target pointing at a public host is refused, so the exception cannot be used to
reach a third-party system.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core import net_guard
from app.core.config import Settings
from app.core.exceptions import TargetNotAuthorizedError
from app.models.registration_models import RegistrationTarget

#: Hosts the bundled demonstration application may live on. A demo target MUST be local.
DEMO_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def ensure_target_usable(target: RegistrationTarget, settings: Settings) -> None:
    """Verify that ``target`` may be tested right now.

    Raises:
        TargetNotAuthorizedError: any gate fails. The message says which one, because "not
            authorized" alone is not actionable.
    """
    if not target.is_active:
        raise TargetNotAuthorizedError(
            f"Target {target.name!r} is inactive.", details={"target_id": target.id}
        )

    if not target.is_authorized or not target.authorization_note:
        raise TargetNotAuthorizedError(
            f"Target {target.name!r} is not authorized, or has no authorization note. "
            "Record who authorized testing it before creating a campaign.",
            details={"target_id": target.id, "is_authorized": target.is_authorized},
        )

    hostname = (urlsplit(target.base_url).hostname or "").lower()

    if target.kind == "demo":
        # The bundled target is exempt from the external allowlist, but only because it is local.
        # This is the property that stops "kind: demo" from becoming an allowlist bypass.
        if hostname not in DEMO_HOSTS:
            raise TargetNotAuthorizedError(
                f"A demo target must be local (one of {sorted(DEMO_HOSTS)}); "
                f"{hostname!r} is not. Register it as an external target and allowlist it instead.",
                details={"hostname": hostname},
            )
        net_guard.validate_outbound_url(target.base_url, allow_private=True, require_https=False)
        return

    net_guard.is_target_authorized(
        target.base_url,
        allowed_hosts=settings.network.allowed_target_hosts,
        allow_private=settings.network.allow_private_targets,
    )


def validate_target_url(base_url: str, *, kind: str, settings: Settings) -> None:
    """Validate a URL at *creation* time, so a bad target never reaches the database.

    Raises:
        SsrfBlockedError / TargetNotAuthorizedError: the URL fails the guard or the policy.
    """
    hostname = (urlsplit(base_url).hostname or "").lower()
    if kind == "demo":
        if hostname not in DEMO_HOSTS:
            raise TargetNotAuthorizedError(
                f"A demo target must be local (one of {sorted(DEMO_HOSTS)}).",
                details={"hostname": hostname},
            )
        net_guard.validate_outbound_url(base_url, allow_private=True)
        return
    net_guard.is_target_authorized(
        base_url,
        allowed_hosts=settings.network.allowed_target_hosts,
        allow_private=settings.network.allow_private_targets,
    )

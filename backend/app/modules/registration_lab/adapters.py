"""Target adapters: how NEXUS drives a registration flow.

The adapter is the only place that knows a specific target's HTTP contract. Everything above it
(campaign executor, analyzer, API) works in terms of outcomes, which is what makes the module
testable and keeps the "stop on a control" rule in one place:

* every adapter returns :class:`AdapterOutcome` values, never exceptions for expected conditions;
* a 429, a CAPTCHA challenge or an access denial maps to a **control** outcome, and callers treat
  those as terminal (docs/SECURITY.md T12);
* no adapter retries around a control, and no adapter has any way to change its identity, proxy or
  rate to get past one.

Two protocol roles from the brief are kept distinct:

* :class:`RegistrationAdapter` - submits and verifies against the target.
* :class:`EmailVerificationAdapter` - obtains the code from the inbox side (implemented in
  ``verification.py`` because it belongs to the mailbox, not the target).

Phase 4 ships the **API adapter** for the bundled demonstration application, which exposes a
documented JSON API. A browser-driving adapter is deliberately absent: the demonstration target
has no HTML UI to drive, so adding Playwright now would be untested code pretending to be a
feature. It arrives when there is an authorized UI target that needs it (see ROADMAP Phase 4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger, redact_string

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings
    from app.models.registration_models import RegistrationTarget

logger = get_logger(__name__)


class AdapterOutcome(StrEnum):
    """What happened when the adapter talked to the target."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"

    # --- controls: the target refused, and NEXUS stops rather than retrying ---
    RATE_LIMITED = "rate_limited"
    CAPTCHA = "captcha"
    ACCESS_DENIED = "access_denied"

    ERROR = "error"

    @property
    def is_control(self) -> bool:
        """True when the target answered with an anti-abuse control."""
        return self in {self.RATE_LIMITED, self.CAPTCHA, self.ACCESS_DENIED}


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    """Normalized result of one adapter call."""

    outcome: AdapterOutcome
    http_status: int | None = None
    message: str | None = None
    elapsed_ms: int | None = None

    @property
    def ok(self) -> bool:
        """True when the call was accepted by the target."""
        return self.outcome is AdapterOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class AccountState:
    """What the target reports about an account."""

    exists: bool
    status: str | None
    detail: str | None = None

    @property
    def is_active(self) -> bool:
        """True when the account is fully activated."""
        return self.exists and (self.status or "").lower() in {"active", "verified", "enabled"}


@runtime_checkable
class RegistrationAdapter(Protocol):
    """Submits and verifies a registration."""

    name: str

    async def submit(self, *, email: str, password: str) -> AdapterResponse:
        """Start a registration for ``email``."""
        ...

    async def verify(self, *, email: str, code: str) -> AdapterResponse:
        """Complete registration with the emailed code."""
        ...

    async def account_status(self, *, email: str) -> AccountState:
        """Report the account's state."""
        ...


#: Test seam: when set, adapters use this transport instead of opening sockets. This is how the
#: integration tests drive the real demonstration application in-process (see Decision D13 for
#: the precedent and the reason a seam is used rather than a live server).
_test_transport: httpx.AsyncBaseTransport | None = None


def set_test_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Install (or clear) the transport used by newly built adapters."""
    global _test_transport  # noqa: PLW0603 - deliberate process-wide test seam
    _test_transport = transport


def _classify(status_code: int, body: str) -> AdapterOutcome:
    """Map an HTTP status and body to an adapter outcome.

    CAPTCHA is checked before the generic 403 because a CAPTCHA challenge is often served as a
    400 or 403 with a challenge in the body, and the distinction matters: one is a control to
    report, the other a denial to report - but neither may ever be retried around.
    """
    lowered = body.lower()
    if "captcha" in lowered or "recaptcha" in lowered or "hcaptcha" in lowered:
        return AdapterOutcome.CAPTCHA
    if status_code == 429:  # noqa: PLR2004
        return AdapterOutcome.RATE_LIMITED
    if status_code in {401, 403}:
        return AdapterOutcome.ACCESS_DENIED
    if status_code == 409:  # noqa: PLR2004
        return AdapterOutcome.DUPLICATE
    if 200 <= status_code < 300:  # noqa: PLR2004
        return AdapterOutcome.ACCEPTED
    if 400 <= status_code < 500:  # noqa: PLR2004
        return AdapterOutcome.REJECTED
    return AdapterOutcome.ERROR


class DemoApiAdapter:
    """Adapter for the bundled demonstration application's JSON API."""

    name = "demo_api"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport if transport is not None else _test_transport

    def _client(self) -> httpx.AsyncClient:
        """Build a client for one call.

        ``trust_env=False`` keeps an environment proxy from silently rerouting registration
        traffic, and redirects are not followed: a target that redirects is telling us something
        changed, which is worth reporting rather than hiding.
        """
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            trust_env=False,
            follow_redirects=False,
            transport=self._transport,
        )

    async def submit(self, *, email: str, password: str) -> AdapterResponse:
        """POST /api/register."""
        return await self._call("POST", "/api/register", {"email": email, "password": password})

    async def verify(self, *, email: str, code: str) -> AdapterResponse:
        """POST /api/verify."""
        return await self._call("POST", "/api/verify", {"email": email, "code": code})

    async def account_status(self, *, email: str) -> AccountState:
        """GET /api/status/{email}."""
        started = time.perf_counter()
        try:
            async with self._client() as client:
                response = await client.get(f"/api/status/{email}")
        except httpx.HTTPError as exc:
            logger.warning(
                "Account status request failed: %s",
                redact_string(str(exc))[:200],
                extra={"module_name": "registration_lab"},
            )
            return AccountState(exists=False, status=None, detail="status request failed")
        if response.status_code == 404:  # noqa: PLR2004
            return AccountState(exists=False, status=None, detail="not found")
        if response.status_code >= 400:  # noqa: PLR2004
            return AccountState(exists=False, status=None, detail=f"http {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return AccountState(exists=False, status=None, detail="unreadable status response")
        del started
        return AccountState(exists=True, status=payload.get("status"))

    async def _call(self, method: str, path: str, payload: dict[str, str]) -> AdapterResponse:
        """Perform one request and normalize the result."""
        started = time.perf_counter()
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=payload)
                body = response.text
        except httpx.HTTPError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return AdapterResponse(
                outcome=AdapterOutcome.ERROR,
                message=redact_string(f"{type(exc).__name__}: {exc}")[:300],
                elapsed_ms=elapsed,
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        outcome = _classify(response.status_code, body)
        return AdapterResponse(
            outcome=outcome,
            http_status=response.status_code,
            message=_short(body),
            elapsed_ms=elapsed,
        )


def _short(text: str, limit: int = 300) -> str | None:
    """Return a short, redacted excerpt for reporting."""
    if not text:
        return None
    return redact_string(text.replace("\n", " ").strip())[:limit]


#: Adapter names that this build can construct. Anything else fails loudly instead of silently
#: falling back to a "best guess" adapter.
SUPPORTED_ADAPTERS: frozenset[str] = frozenset({"demo_api"})


def build_adapter(target: RegistrationTarget, settings: Settings) -> RegistrationAdapter:
    """Construct the adapter for a target.

    Raises:
        NotFoundError: the target names an adapter this build does not provide.
    """
    if target.adapter_name in SUPPORTED_ADAPTERS:
        return DemoApiAdapter(
            target.base_url,
            timeout_seconds=max(settings.network.http_timeout_seconds, 5.0),
        )
    raise NotFoundError(
        f"No adapter named {target.adapter_name!r} is available.",
        details={"supported": sorted(SUPPORTED_ADAPTERS)},
    )


async def ensure_reachable(adapter: RegistrationAdapter) -> None:
    """Raise when a target cannot be reached at all.

    Kept separate from the outcome flow: a transport failure on the *first* call is a setup
    problem (wrong URL, target down), which should surface as an error rather than as a hundred
    per-test "unknown error" rows.
    """
    probe = await adapter.account_status(email="nonexistent@example.test")
    del probe

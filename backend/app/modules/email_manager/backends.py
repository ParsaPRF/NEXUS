"""Mail backends: the adapter seam between NEXUS and the test-mail infrastructure.

Two implementations, one protocol:

* :class:`InMemoryMailBackend` - messages live in a dictionary. Used by the automated tests and
  by anyone working offline. It cannot receive real SMTP mail, which is the point: nothing can
  leak in or out.
* :class:`MailpitBackend` - talks to Mailpit over its HTTP API. Mailpit is a local SMTP sink that
  accepts messages and *cannot deliver them anywhere*, so it is the correct backend for
  disposable test inboxes.

**Why polling and not an SMTP listener inside NEXUS?** Mailpit already owns the SMTP port and the
mailbox store; parsing SMTP ourselves would duplicate it and create two sources of truth. NEXUS
therefore *reads* Mailpit. Both halves are still local, and neither can reach a third-party
provider - which is the property that matters (docs/SECURITY.md 3).

The backend URL is trusted configuration (the same reasoning as the proxy check URL, decision
D12): it is set by the operator in the environment file, not supplied by an API caller, so it is
not run through the SSRF guard.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

from app.core.exceptions import ExternalServiceError, ValidationError
from app.core.logging import get_logger, redact_string

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings

logger = get_logger(__name__)

#: Hard ceiling on how many message ids one poll asks for.
MAX_LIST_LIMIT = 200


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message as delivered by a mail backend, before it is stored."""

    backend_id: str
    sender: str
    recipient: str
    subject: str | None
    body_text: str | None
    body_html: str | None
    received_at: datetime
    size_bytes: int | None = None
    headers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class MailBackend(Protocol):
    """The contract every mail backend implements."""

    async def list_ids(self, recipient: str, *, limit: int = MAX_LIST_LIMIT) -> list[str]:
        """Return backend message ids addressed to ``recipient``, newest first."""
        ...

    async def fetch(self, backend_id: str) -> InboundMessage | None:
        """Return one message, or ``None`` when it no longer exists."""
        ...

    async def health(self) -> bool:
        """Return ``True`` when the backend is reachable."""
        ...


# --------------------------------------------------------------------------------------
# In-memory backend
# --------------------------------------------------------------------------------------


class InMemoryMailBackend:
    """A mail backend that keeps messages in memory.

    The ``deliver`` method is intentionally synchronous and public: tests use it to simulate
    "the application sent a verification email", and nothing else can call it because it is not
    exposed over the API.
    """

    def __init__(self) -> None:
        self._messages: dict[str, InboundMessage] = {}

    # -- test/simulation hook ---------------------------------------------------------

    def deliver(
        self,
        *,
        recipient: str,
        sender: str = "no-reply@demo.test",
        subject: str = "Your verification code",
        body_text: str | None = None,
        body_html: str | None = None,
        received_at: datetime | None = None,
        backend_id: str | None = None,
    ) -> str:
        """Store a message and return its backend id."""
        message_id = backend_id or uuid.uuid4().hex
        message = InboundMessage(
            backend_id=message_id,
            sender=sender,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            received_at=received_at or datetime.now(UTC),
            size_bytes=len((body_text or "").encode()) + len((body_html or "").encode()),
        )
        self._messages[message_id] = message
        return message_id

    # -- MailBackend ------------------------------------------------------------------

    async def list_ids(self, recipient: str, *, limit: int = MAX_LIST_LIMIT) -> list[str]:
        """Return ids addressed to ``recipient``, newest first."""
        target = recipient.strip().lower()
        matched = [
            message
            for message in self._messages.values()
            if message.recipient.strip().lower() == target
        ]
        matched.sort(key=lambda message: message.received_at, reverse=True)
        return [message.backend_id for message in matched[: min(limit, MAX_LIST_LIMIT)]]

    async def fetch(self, backend_id: str) -> InboundMessage | None:
        """Return one stored message."""
        return self._messages.get(backend_id)

    async def health(self) -> bool:
        """The in-memory backend is always available."""
        return True

    # -- helpers ----------------------------------------------------------------------

    def clear(self) -> None:
        """Drop every message. Used by tests."""
        self._messages.clear()

    @property
    def message_count(self) -> int:
        """Number of stored messages."""
        return len(self._messages)


# --------------------------------------------------------------------------------------
# Mailpit backend
# --------------------------------------------------------------------------------------


class MailpitBackend:
    """Reads messages from a local Mailpit instance over its HTTP API."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = settings.mail.api_url.rstrip("/")
        self._timeout = httpx.Timeout(settings.network.http_timeout_seconds)
        self._max_bytes = settings.mail.max_message_bytes
        #: Test seam: an injected transport answers every request, so parsing can be tested
        #: without a running Mailpit.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        """Build a client with a timeout and no environment proxy inheritance."""
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            trust_env=False,
            transport=self._transport,
        )

    async def list_ids(self, recipient: str, *, limit: int = MAX_LIST_LIMIT) -> list[str]:
        """Ask Mailpit for message ids addressed to ``recipient``.

        Mailpit's search syntax is ``to:<address>``. The query is sent as a parameter, so httpx
        encodes it - no string concatenation into a request line.

        Raises:
            ExternalServiceError: Mailpit is unreachable or returned an unusable response.
        """
        capped = min(limit, MAX_LIST_LIMIT)
        try:
            async with self._client() as client:
                response = await client.get(
                    "/api/v1/search",
                    params={"query": f"to:{recipient}", "limit": capped},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise ExternalServiceError(
                "The mail backend rejected the search request.",
                details={"status": exc.response.status_code},
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalServiceError(
                "The mail backend could not be reached.",
                details={"reason": redact_string(str(exc))[:200]},
            ) from exc

        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            raise ExternalServiceError("The mail backend returned an unexpected search result.")
        ids: list[str] = []
        for item in messages:
            if isinstance(item, dict) and (message_id := item.get("ID")):
                ids.append(str(message_id))
        return ids

    async def fetch(self, backend_id: str) -> InboundMessage | None:
        """Fetch one message, or ``None`` when Mailpit no longer has it.

        Raises:
            ExternalServiceError: Mailpit is unreachable.
        """
        try:
            async with self._client() as client:
                response = await client.get(f"/api/v1/message/{backend_id}")
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                content = response.content
        except httpx.HTTPStatusError as exc:
            raise ExternalServiceError(
                "The mail backend rejected the message request.",
                details={"status": exc.response.status_code},
            ) from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                "The mail backend could not be reached.",
                details={"reason": redact_string(str(exc))[:200]},
            ) from exc

        if len(content) > self._max_bytes:
            # A message larger than the cap is skipped rather than loaded into the database.
            logger.warning(
                "Skipping message %s: %d bytes exceeds the %d byte cap",
                backend_id,
                len(content),
                self._max_bytes,
                extra={"module_name": "email_manager"},
            )
            return None

        try:
            payload = httpx.Response(200, content=content).json()
        except ValueError:
            return None
        return _message_from_mailpit(payload, backend_id)

    async def health(self) -> bool:
        """Return ``True`` when Mailpit answers its info endpoint."""
        try:
            async with self._client() as client:
                response = await client.get("/api/v1/info")
                return response.status_code < 500  # noqa: PLR2004
        except httpx.HTTPError:
            return False


def _message_from_mailpit(payload: dict[str, Any], backend_id: str) -> InboundMessage | None:
    """Translate one Mailpit message payload into :class:`InboundMessage`.

    Mailpit's JSON shape has changed across versions (``From`` may be a string or an object,
    ``To`` may be a list of objects), so each field is read defensively: a missing or oddly
    shaped field degrades that field, it does not lose the message.
    """
    if not isinstance(payload, dict):
        return None

    sender = _address_of(payload.get("From"))
    recipients = payload.get("To")
    recipient = ""
    if isinstance(recipients, list) and recipients:
        recipient = _address_of(recipients[0])
    elif isinstance(recipients, str):
        recipient = recipients

    received_at = _parse_timestamp(payload.get("Created"))
    text = payload.get("Text")
    html = payload.get("HTML")

    return InboundMessage(
        backend_id=str(payload.get("ID") or backend_id),
        sender=sender,
        recipient=recipient,
        subject=payload.get("Subject") or None,
        body_text=text if isinstance(text, str) else None,
        body_html=html if isinstance(html, str) else None,
        received_at=received_at,
        size_bytes=payload.get("Size") if isinstance(payload.get("Size"), int) else None,
    )


def _address_of(value: Any) -> str:
    """Extract a bare address from Mailpit's string-or-object representation."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        address = value.get("Address") or value.get("address")
        return str(address) if address else ""
    return ""


def _parse_timestamp(value: Any) -> datetime:
    """Parse Mailpit's ``Created`` timestamp, falling back to now.

    A wrong timestamp is recoverable (the message still exists); dropping the message because its
    date was unparseable is not.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------------------

_backend: MailBackend | None = None


def get_backend(settings: Settings) -> MailBackend:
    """Return the process-wide mail backend, creating it on first use."""
    global _backend  # noqa: PLW0603 - module-level singleton, same pattern as the job queue
    if _backend is not None:
        return _backend
    if settings.mail.backend == "mailpit":
        _backend = MailpitBackend(settings)
    else:
        _backend = InMemoryMailBackend()
    return _backend


def reset_backend() -> None:
    """Forget the current backend. Used at shutdown and between tests."""
    global _backend  # noqa: PLW0603
    _backend = None


async def backend_health(settings: Settings) -> bool:
    """Report whether the configured backend answers."""
    try:
        return await asyncio.wait_for(get_backend(settings).health(), timeout=5.0)
    except (TimeoutError, ExternalServiceError):
        return False


def validate_backend_name(name: str) -> str:
    """Validate a backend name from configuration.

    Raises:
        ValidationError: the name is not ``memory`` or ``mailpit``.
    """
    if name not in {"memory", "mailpit"}:
        raise ValidationError(
            f"Unknown mail backend {name!r}.", details={"allowed": ["memory", "mailpit"]}
        )
    return name

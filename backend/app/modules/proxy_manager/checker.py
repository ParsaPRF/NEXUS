"""Proxy health checking.

What a "check" means here: can this proxy carry an HTTP request to a **configured endpoint the
operator owns**? That is deliberately narrow. NEXUS does not probe arbitrary third-party services
to grade proxies, because doing so both violates the ownership rule and turns the tool into a
load source that nobody agreed to.

How the check is performed:

1. The proxy URL is built with decrypted credentials (or none).
2. ``httpx.AsyncClient`` is created with that proxy, ``trust_env=False`` (so a system-wide proxy
   variable cannot silently change what is being measured) and an explicit timeout.
3. The response is **not** read. ``send(..., stream=True)`` plus an immediate close means a
   hostile or misconfigured endpoint cannot make us download a large body (threat T7). Receiving
   any HTTP status at all proves the proxy carried traffic.
4. Elapsed time is measured around the request and recorded as latency.
5. Failures are classified, redacted, and returned as data - never raised into the caller.

Conqueue: fan-out is bounded by ``max_concurrent_checks``. Unbounded parallel checks would turn
the host into a small denial-of-service source, which is exactly the behaviour the project
refuses (threat T12).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from app.core.exceptions import CredentialsNotStorableError
from app.core.logging import get_logger, redact_string
from app.models.proxy_models import Proxy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings
    from app.core.security import CredentialCipher

logger = get_logger(__name__)

#: Error classes recorded in ``proxy_checks.error_class``. Stable strings, part of the contract.
ERROR_TIMEOUT = "timeout"
ERROR_CONNECT = "connect_error"
ERROR_PROXY = "proxy_error"
ERROR_TLS = "tls_error"
ERROR_HTTP = "http_error"
ERROR_UNKNOWN = "unknown"

_MAX_ERROR_MESSAGE = 300

#: Fragments that mark a failure as a TLS problem rather than a plain connection error.
_TLS_MARKERS = ("ssl", "tls", "certificate", "cert verify", "handshake")


def _classify_http_error(exc: httpx.HTTPError) -> str:
    """Map an httpx transport failure to one of the documented error classes."""
    text = str(exc).lower()
    if any(marker in text for marker in _TLS_MARKERS):
        return ERROR_TLS
    if isinstance(exc, httpx.ConnectError):
        return ERROR_CONNECT
    return ERROR_UNKNOWN


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """The result of one health-check attempt.

    ``ok`` means "the proxy delivered the request", not "the endpoint returned 200". An endpoint
    answering 503 through a working proxy is a working proxy.
    """

    ok: bool
    latency_ms: int | None = None
    http_status: int | None = None
    error_class: str | None = None
    error_message: str | None = None


class ProxyHealthChecker:
    """Checks proxies against one configured endpoint."""

    def __init__(
        self,
        settings: Settings,
        cipher: CredentialCipher | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        #: Test seam. Injecting a transport lets the classification logic be tested without a
        #: real socket - and without ever pointing the checker at a third-party service.
        self._transport = transport

    # ------------------------------------------------------------------ URL building

    def build_proxy_url(self, proxy: Proxy) -> str:
        """Build the ``scheme://[user:pass@]host:port`` URL httpx needs.

        Raises:
            CredentialsNotStorableError: the proxy has a stored password but no usable cipher,
                which means the credential key is missing or changed. Failing loudly beats
                silently checking without credentials and reporting a confusing failure.
        """
        host = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
        if not proxy.username:
            return f"{proxy.protocol}://{host}:{proxy.port}"

        password = ""
        if proxy.password_encrypted:
            if self._cipher is None:
                raise CredentialsNotStorableError(
                    "Stored proxy credentials cannot be used because the credential key is not "
                    "configured.",
                    details={"proxy_id": proxy.id},
                )
            password = self._cipher.decrypt(proxy.password_encrypted)
        return f"{proxy.protocol}://{proxy.username}:{password}@{host}:{proxy.port}"

    # ------------------------------------------------------------------ checking

    def _build_client(self, proxy_url: str) -> httpx.AsyncClient:
        """Create the HTTP client for one check.

        ``trust_env=False`` matters: without it an ``HTTP_PROXY`` variable in the environment
        would silently reroute the request and the measured latency would describe the wrong path.

        The ``transport`` seam: httpx resolves ``proxy=`` into its own proxy transport and
        ignores any transport passed alongside it, so an injected transport cannot coexist with a
        proxy. When a transport is injected the proxy is therefore omitted, and the transport
        stands in for "whatever the proxy would have answered". That keeps the classification
        logic testable without opening a socket.
        """
        common: dict[str, object] = {
            "timeout": httpx.Timeout(self._settings.network.http_timeout_seconds),
            "trust_env": False,
            "follow_redirects": False,
        }
        if self._transport is not None:
            return httpx.AsyncClient(transport=self._transport, **common)  # type: ignore[arg-type]
        return httpx.AsyncClient(proxy=proxy_url, **common)  # type: ignore[arg-type]

    async def check(self, proxy: Proxy) -> CheckOutcome:
        """Check one proxy against ``settings.proxy.check_url``."""
        try:
            proxy_url = self.build_proxy_url(proxy)
        except CredentialsNotStorableError as exc:
            return CheckOutcome(ok=False, error_class=ERROR_UNKNOWN, error_message=exc.message)

        check_url = self._settings.proxy.check_url
        started = time.perf_counter()
        try:
            client = self._build_client(proxy_url)
            async with client:
                request = client.build_request("GET", check_url)
                response = await client.send(request, stream=True)
                status = response.status_code
                # Close without reading the body: the point is connectivity, not content.
                await response.aclose()
        except httpx.TimeoutException as exc:
            return self._failure(ERROR_TIMEOUT, exc, started)
        except httpx.ProxyError as exc:
            return self._failure(ERROR_PROXY, exc, started)
        except httpx.HTTPError as exc:
            # Covers ConnectError, TransportError and friends. TLS failures arrive as a
            # ConnectError whose text mentions the certificate, so classify from the text rather
            # than from the exception type - checking ``isinstance`` first would report a
            # certificate problem as a plain connection failure.
            return self._failure(_classify_http_error(exc), exc, started)
        except OSError as exc:
            return self._failure(ERROR_CONNECT, exc, started)

        latency_ms = int((time.perf_counter() - started) * 1000)
        return CheckOutcome(ok=True, latency_ms=latency_ms, http_status=status)

    def _failure(self, error_class: str, exc: BaseException, started: float) -> CheckOutcome:
        """Build a failure outcome with a redacted message.

        Exception text routinely embeds the request URL - which, for a proxied request, contains
        the proxy password. ``redact_string`` masks it (threat T3).
        """
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        message = redact_string(f"{type(exc).__name__}: {exc}")[:_MAX_ERROR_MESSAGE]
        return CheckOutcome(
            ok=False,
            latency_ms=elapsed_ms,
            error_class=error_class,
            error_message=message,
        )

    async def check_many(self, proxies: list[Proxy]) -> list[tuple[Proxy, CheckOutcome]]:
        """Check several proxies concurrently, bounded by ``max_concurrent_checks``.

        Returns results in the same order as the input, which makes the caller's bookkeeping and
        the tests straightforward. Errors are already contained inside each outcome.
        """
        semaphore = asyncio.Semaphore(self._settings.network.max_concurrent_checks)

        async def _one(proxy: Proxy) -> tuple[Proxy, CheckOutcome]:
            async with semaphore:
                return proxy, await self.check(proxy)

        return list(await asyncio.gather(*(_one(proxy) for proxy in proxies)))

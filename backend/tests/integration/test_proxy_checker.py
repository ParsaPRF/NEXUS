"""Health-checker tests.

No sockets are opened. The checker accepts an injected ``httpx`` transport, which is the test
seam: ``MockTransport`` lets us simulate a working proxy, a timeout, a refusal and a TLS failure
without touching the network - and crucially without ever pointing the checker at a third-party
service to "see if it works".
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.exceptions import CredentialsNotStorableError
from app.core.security import CredentialCipher
from app.models.proxy_models import Proxy
from app.modules.proxy_manager.checker import (
    ERROR_CONNECT,
    ERROR_PROXY,
    ERROR_TIMEOUT,
    ERROR_TLS,
    ERROR_UNKNOWN,
    ProxyHealthChecker,
)


def _proxy(**overrides: object) -> Proxy:
    """Build a transient Proxy (no database session involved)."""
    defaults: dict[str, object] = {
        "id": 1,
        "protocol": "http",
        "host": "proxy.example.test",
        "port": 8080,
        "success_count": 0,
        "failure_count": 0,
        "consecutive_failures": 0,
        "is_enabled": True,
    }
    defaults.update(overrides)
    return Proxy(**defaults)  # type: ignore[arg-type]


def _cipher(settings) -> CredentialCipher:  # type: ignore[no-untyped-def]
    return CredentialCipher(settings.proxy_credential_key)


class TestProxyUrlBuilding:
    """The URL handed to httpx, including credentials."""

    def test_no_credentials(self, test_settings) -> None:
        checker = ProxyHealthChecker(test_settings)
        assert checker.build_proxy_url(_proxy()) == "http://proxy.example.test:8080"

    def test_credentials_are_decrypted(self, test_settings) -> None:
        cipher = _cipher(test_settings)
        proxy = _proxy(username="alice", password_encrypted=cipher.encrypt("supersecret"))
        checker = ProxyHealthChecker(test_settings, cipher)
        url = checker.build_proxy_url(proxy)
        assert url == "http://alice:supersecret@proxy.example.test:8080"

    def test_socks5_scheme_is_preserved(self, test_settings) -> None:
        checker = ProxyHealthChecker(test_settings)
        assert checker.build_proxy_url(_proxy(protocol="socks5", port=1080)).startswith("socks5://")

    def test_ipv6_host_is_bracketed(self, test_settings) -> None:
        checker = ProxyHealthChecker(test_settings)
        assert checker.build_proxy_url(_proxy(host="2001:db8::1")) == ("http://[2001:db8::1]:8080")

    def test_stored_credentials_without_a_cipher_are_refused(self, test_settings) -> None:
        # The credential key is missing or changed. Checking without the password would produce a
        # confusing auth failure, so this raises a clear, actionable error instead.
        proxy = _proxy(username="alice", password_encrypted="whatever")
        checker = ProxyHealthChecker(test_settings, cipher=None)
        with pytest.raises(CredentialsNotStorableError):
            checker.build_proxy_url(proxy)


class TestCheckOutcomes:
    """Classification of success and of each failure mode."""

    async def test_success_records_status_and_latency(self, test_settings) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        checker = ProxyHealthChecker(test_settings, transport=transport)

        outcome = await checker.check(_proxy())
        assert outcome.ok is True
        assert outcome.http_status == 200
        assert outcome.latency_ms is not None
        assert outcome.latency_ms >= 0
        assert outcome.error_class is None

    async def test_an_error_status_still_counts_as_a_working_proxy(self, test_settings) -> None:
        # The check endpoints returning 503 proves the proxy carried the request. Reporting that as
        # a dead proxy would be wrong.
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        checker = ProxyHealthChecker(test_settings, transport=transport)

        outcome = await checker.check(_proxy())
        assert outcome.ok is True
        assert outcome.http_status == 503

    async def test_timeout_is_classified(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out", request=request)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        outcome = await checker.check(_proxy())
        assert outcome.ok is False
        assert outcome.error_class == ERROR_TIMEOUT
        assert outcome.error_message

    async def test_connection_refused_is_classified(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=request)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        outcome = await checker.check(_proxy())
        assert outcome.ok is False
        assert outcome.error_class == ERROR_CONNECT

    async def test_tls_failure_is_classified_separately(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "SSL: CERTIFICATE_VERIFY_FAILED certificate verify failed", request=request
            )

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        outcome = await checker.check(_proxy())
        assert outcome.error_class == ERROR_TLS

    async def test_proxy_error_is_classified(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ProxyError("proxy authentication required", request=request)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        outcome = await checker.check(_proxy())
        assert outcome.error_class == ERROR_PROXY

    async def test_other_transport_error_is_unknown(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TransportError("something odd", request=request)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        outcome = await checker.check(_proxy())
        assert outcome.error_class == ERROR_UNKNOWN

    async def test_os_error_is_treated_as_a_connection_failure(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise OSError("no route to host")

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        outcome = await checker.check(_proxy())
        assert outcome.error_class == ERROR_CONNECT

    async def test_credentials_in_an_error_message_are_redacted(self, test_settings) -> None:
        # httpx exception text can embed the request URL, which contains the proxy password.
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "failed connecting to http://alice:supersecret@proxy.example.test:8080",
                request=request,
            )

        cipher = _cipher(test_settings)
        proxy = _proxy(username="alice", password_encrypted=cipher.encrypt("supersecret"))
        checker = ProxyHealthChecker(test_settings, cipher, transport=httpx.MockTransport(handler))

        outcome = await checker.check(proxy)
        assert outcome.ok is False
        assert "supersecret" not in (outcome.error_message or "")
        assert "***REDACTED***" in (outcome.error_message or "")

    async def test_a_failed_check_still_records_elapsed_time(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        outcome = await checker.check(_proxy())
        # Knowing how long it took to fail is useful when diagnosing a flaky pool.
        assert outcome.latency_ms is not None


class TestCheckMany:
    """Fan-out is bounded and results keep their order."""

    async def test_results_are_in_input_order(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        proxies = [_proxy(id=index, host=f"p{index}.example.test") for index in range(1, 5)]

        results = await checker.check_many(proxies)
        assert [proxy.id for proxy, _ in results] == [1, 2, 3, 4]

    async def test_empty_list_returns_empty(self, test_settings) -> None:
        checker = ProxyHealthChecker(test_settings)
        assert await checker.check_many([]) == []

    async def test_concurrency_is_bounded(self, test_settings) -> None:
        # test_settings sets max_concurrent_checks=2. Unbounded fan-out is exactly the behaviour
        # the project refuses (threat T12), so the cap must be real.
        state = {"running": 0, "peak": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.03)
            state["running"] -= 1
            return httpx.Response(200)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        proxies = [_proxy(id=index) for index in range(8)]
        await checker.check_many(proxies)

        assert state["peak"] <= test_settings.network.max_concurrent_checks

    async def test_one_failure_does_not_abort_the_batch(self, test_settings) -> None:
        # The injected transport sees the *check endpoint* URL, not the proxy address, so failure
        # is simulated by call count. Which specific proxy fails is therefore not asserted - what
        # matters is that one failure neither aborts nor corrupts the rest of the batch.
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 2:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200)

        checker = ProxyHealthChecker(test_settings, transport=httpx.MockTransport(handler))
        proxies = [_proxy(id=index) for index in (1, 2, 3)]
        results = await checker.check_many(proxies)

        assert len(results) == 3
        assert [outcome.ok for _, outcome in results].count(False) == 1
        assert [outcome.ok for _, outcome in results].count(True) == 2

"""API client tests (no Qt involved).

These are the tests that matter most for the desktop application: everything that can be wrong
about talking to the backend - headers, error translation, parsing, query building - is not a
widget, and is verified here in milliseconds.
"""

from __future__ import annotations

import httpx
import pytest

from app.api_client import ApiError, NexusApiError, NexusClient
from app.config import DesktopConfig
from tests.conftest import TEST_API_KEY
from tests.fake_api import FakeApi


class TestRequests:
    """Requests are built correctly."""

    def test_sends_the_api_key_header(self, config: DesktopConfig, fake_api: FakeApi) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return httpx.Response(200, json={"status": "ok"})

        client = NexusClient(config, transport=httpx.MockTransport(handler))
        client.health()
        assert seen["x-api-key"] == TEST_API_KEY
        client.close()

    def test_omits_the_header_when_no_key(self, fake_api: FakeApi) -> None:
        headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            headers.update(dict(request.headers))
            return httpx.Response(200, json={"status": "ok"})

        client = NexusClient(
            DesktopConfig(base_url="http://nexus.test", api_key=""),
            transport=httpx.MockTransport(handler),
        )
        client.health()
        assert "x-api-key" not in headers
        client.close()

    def test_query_parameters_are_sent(self, config: DesktopConfig, fake_api: FakeApi) -> None:
        captured: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url)
            return httpx.Response(200, json={"items": [], "meta": {}})

        client = NexusClient(config, transport=httpx.MockTransport(handler))
        client.list_proxies(limit=50, status="healthy")
        assert captured[0].params["limit"] == "50"
        assert captured[0].params["status"] == "healthy"
        client.close()

    def test_empty_parameters_are_dropped(self, config: DesktopConfig) -> None:
        captured: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url)
            return httpx.Response(200, json={"items": [], "meta": {}})

        client = NexusClient(config, transport=httpx.MockTransport(handler))
        client.list_proxies(limit=50, status=None, search="")
        assert "status" not in captured[0].params
        assert "search" not in captured[0].params
        client.close()

    def test_json_bodies_are_sent(self, config: DesktopConfig) -> None:
        bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content)
            return httpx.Response(201, json={"public_id": "x"})

        client = NexusClient(config, transport=httpx.MockTransport(handler))
        client.create_inbox(label="hello")
        assert b"hello" in bodies[0]
        client.close()


class TestResponses:
    """Responses are translated into Python values."""

    def test_json_is_parsed(self, client: NexusClient) -> None:
        info = client.instance_info()
        assert info["app"] == "NEXUS"

    def test_text_responses_are_returned_as_text(self, client: NexusClient) -> None:
        content = client.export_proxies(fmt="csv")
        assert content.startswith("protocol,host,port")

    def test_no_content_returns_none(self, config: DesktopConfig) -> None:
        client = NexusClient(
            config, transport=httpx.MockTransport(lambda request: httpx.Response(204))
        )
        assert client.delete_proxy(1) is None
        client.close()

    def test_lists_are_parsed(self, client: NexusClient) -> None:
        targets = client.list_targets()
        assert isinstance(targets, list)
        assert targets[0]["name"] == "Bundled demo application"


class TestErrors:
    """Every failure becomes a NexusApiError carrying the backend's code."""

    def test_error_body_is_parsed(self, config: DesktopConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={
                    "code": "ssrf_blocked",
                    "message": "The requested URL is not permitted.",
                    "request_id": "abc123",
                },
            )

        client = NexusClient(config, transport=httpx.MockTransport(handler))
        with pytest.raises(NexusApiError) as excinfo:
            client.list_proxies()
        error = excinfo.value.error
        assert error.code == "ssrf_blocked"
        assert error.status == 403  # noqa: PLR2004
        assert error.request_id == "abc123"
        assert "abc123" in str(error)
        client.close()

    def test_authentication_error_is_flagged(self, config: DesktopConfig) -> None:
        client = NexusClient(
            config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401, json={"code": "unauthenticated", "message": "A valid API key is required."}
                )
            ),
        )
        with pytest.raises(NexusApiError) as excinfo:
            client.instance_info()
        assert excinfo.value.error.is_auth_error is True
        client.close()

    def test_non_json_error_degrades_gracefully(self, config: DesktopConfig) -> None:
        # A proxy or a crashed process may answer with HTML; that must not raise a parse error.
        client = NexusClient(
            config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(502, content=b"<html>bad gateway</html>")
            ),
        )
        with pytest.raises(NexusApiError) as excinfo:
            client.health()
        assert excinfo.value.error.code == "http_502"
        client.close()

    def test_connection_failure_is_reported_as_unreachable(self, config: DesktopConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = NexusClient(config, transport=httpx.MockTransport(handler))
        with pytest.raises(NexusApiError) as excinfo:
            client.health()
        error = excinfo.value.error
        assert error.is_connection_error is True
        assert "Is the backend running?" in error.message
        client.close()

    def test_timeout_is_reported(self, config: DesktopConfig) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("too slow", request=request)

        client = NexusClient(config, transport=httpx.MockTransport(handler))
        with pytest.raises(NexusApiError) as excinfo:
            client.health()
        assert excinfo.value.error.code == "timeout"
        client.close()

    def test_missing_route_is_a_normal_api_error(self, fake_api: FakeApi) -> None:
        client = NexusClient(
            DesktopConfig(base_url="http://nexus.test", api_key="k"),
            transport=fake_api.transport(),
        )
        with pytest.raises(NexusApiError) as excinfo:
            client._get("/api/v1/does-not-exist")  # noqa: SLF001 - exercising the plumbing
        assert excinfo.value.error.status == 404  # noqa: PLR2004
        client.close()


class TestConfiguration:
    """Configuration is read from the environment."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("NEXUS_DESKTOP_API_URL", "NEXUS_DESKTOP_API_KEY", "NEXUS_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        config = DesktopConfig.from_environment()
        assert config.base_url == "http://127.0.0.1:8000"

    def test_reads_desktop_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DESKTOP_API_URL", "http://127.0.0.1:9999/")
        monkeypatch.setenv("NEXUS_DESKTOP_API_KEY", "abc")
        config = DesktopConfig.from_environment()
        # A trailing slash would produce doubled slashes in every request path.
        assert config.base_url == "http://127.0.0.1:9999"
        assert config.api_key == "abc"

    def test_falls_back_to_the_backend_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_DESKTOP_API_KEY", raising=False)
        monkeypatch.setenv("NEXUS_API_KEY", "shared-key")
        assert DesktopConfig.from_environment().api_key == "shared-key"

    def test_tls_verification_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DESKTOP_VERIFY_TLS", "false")
        assert DesktopConfig.from_environment().verify_tls is False


class TestApiErrorRendering:
    """The error object renders usefully in a status bar."""

    def test_message_with_request_id(self) -> None:
        error = ApiError(code="x", message="Something failed", request_id="r1")
        assert str(error) == "Something failed (request r1)"

    def test_message_without_request_id(self) -> None:
        assert str(ApiError(code="x", message="Something failed")) == "Something failed"

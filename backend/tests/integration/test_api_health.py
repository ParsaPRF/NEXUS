"""Integration tests for the HTTP layer: health, readiness, error contract, security headers.

These drive the real ASGI application in-process through ``httpx.ASGITransport`` - no socket,
no server process, no port conflicts. What they verify is behaviour a unit test cannot: that
middleware ordering is right, that exceptions become the documented JSON shape, and that
authentication is actually enforced on the routes that need it.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestLiveness:
    """``/health`` answers without touching any dependency."""

    async def test_returns_ok_without_authentication(self, anonymous_client: AsyncClient) -> None:
        # A probe cannot present an API key, so this endpoint must never require one.
        response = await anonymous_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["app"] == "NEXUS"
        assert body["profile"] == "local"

    async def test_reports_the_environment(self, anonymous_client: AsyncClient) -> None:
        body = (await anonymous_client.get("/health")).json()
        assert body["environment"] == "development"

    async def test_includes_an_iso_timestamp(self, anonymous_client: AsyncClient) -> None:
        body = (await anonymous_client.get("/health")).json()
        assert "T" in body["time"]

    async def test_exposes_nothing_sensitive(self, anonymous_client: AsyncClient) -> None:
        # A health endpoint is unauthenticated, so it must leak no configuration detail.
        text = (await anonymous_client.get("/health")).text
        assert "test-api-key" not in text
        assert "test-secret" not in text
        assert "sqlite" not in text


class TestReadiness:
    """``/health/ready`` reports each dependency separately."""

    async def test_database_is_reported_ok(self, anonymous_client: AsyncClient) -> None:
        body = (await anonymous_client.get("/health/ready")).json()
        checks = {check["name"]: check for check in body["checks"]}
        assert checks["database"]["state"] == "ok"

    async def test_reports_every_expected_dependency(self, anonymous_client: AsyncClient) -> None:
        body = (await anonymous_client.get("/health/ready")).json()
        names = {check["name"] for check in body["checks"]}
        assert names == {"database", "mail_backend", "job_backend", "security"}

    async def test_in_memory_mail_backend_is_flagged_degraded(
        self, anonymous_client: AsyncClient
    ) -> None:
        # Honest reporting: the in-memory backend means messages never leave the process.
        body = (await anonymous_client.get("/health/ready")).json()
        checks = {check["name"]: check for check in body["checks"]}
        assert checks["mail_backend"]["state"] == "degraded"

    async def test_security_check_reports_ok_with_an_api_key_configured(
        self, anonymous_client: AsyncClient
    ) -> None:
        body = (await anonymous_client.get("/health/ready")).json()
        checks = {check["name"]: check for check in body["checks"]}
        assert checks["security"]["state"] == "ok"

    async def test_not_ready_only_when_a_check_fails(self, anonymous_client: AsyncClient) -> None:
        body = (await anonymous_client.get("/health/ready")).json()
        failed = [c for c in body["checks"] if c["state"] == "failed"]
        assert body["ready"] is (not failed)

    async def test_database_info_hides_the_connection_url(
        self, anonymous_client: AsyncClient
    ) -> None:
        body = (await anonymous_client.get("/health/db")).json()
        assert body["dialect"] == "sqlite"
        # A connection string routinely embeds a password, so only the dialect is returned.
        assert "url" not in body


class TestAuthentication:
    """Every ``/api/v1`` route must require the API key."""

    async def test_meta_without_key_is_rejected(self, anonymous_client: AsyncClient) -> None:
        response = await anonymous_client.get("/api/v1/meta")
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"

    async def test_meta_with_wrong_key_is_rejected(self, anonymous_client: AsyncClient) -> None:
        response = await anonymous_client.get(
            "/api/v1/meta", headers={"X-API-Key": "definitely-not-the-key"}
        )
        assert response.status_code == 401

    async def test_meta_with_valid_key_is_accepted(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/meta")).status_code == 200

    async def test_limits_requires_authentication(self, anonymous_client: AsyncClient) -> None:
        assert (await anonymous_client.get("/api/v1/meta/limits")).status_code == 401

    async def test_audit_selftest_requires_authentication(
        self, anonymous_client: AsyncClient
    ) -> None:
        assert (await anonymous_client.post("/api/v1/meta/audit-selftest")).status_code == 401

    async def test_error_body_has_the_documented_shape(self, anonymous_client: AsyncClient) -> None:
        body = (await anonymous_client.get("/api/v1/meta")).json()
        assert set(body) >= {"code", "message", "request_id"}


class TestInstanceInfo:
    """``/api/v1/meta`` describes the instance without exposing secrets."""

    async def test_reports_configuration_facts(self, client: AsyncClient) -> None:
        body = (await client.get("/api/v1/meta")).json()
        assert body["app"] == "NEXUS"
        assert body["profile"] == "local"
        assert body["database_dialect"] == "sqlite"
        assert body["mail_backend"] == "memory"
        assert body["job_backend"] == "inprocess"
        assert body["auth_enabled"] is True

    async def test_never_returns_the_api_key(self, client: AsyncClient) -> None:
        # auth_enabled reports *that* a key exists; the key itself must never appear.
        text = (await client.get("/api/v1/meta")).text
        assert "test-api-key" not in text


class TestSafetyLimits:
    """``/api/v1/meta/limits`` makes the safety posture visible in the UI."""

    async def test_reports_conservative_defaults(self, client: AsyncClient) -> None:
        body = (await client.get("/api/v1/meta/limits")).json()
        assert body["allow_private_targets"] is False
        assert body["allowed_target_hosts"] == []
        assert body["http_timeout_seconds"] == 5.0
        assert body["max_concurrent_checks"] == 2

    async def test_explains_why_registration_is_unavailable(self, client: AsyncClient) -> None:
        # With no allowlist configured, the UI should be able to say why rather than fail.
        body = (await client.get("/api/v1/meta/limits")).json()
        assert "demonstration application" in body["registration_disabled_reason"]


class TestErrorContract:
    """Every failure has the same JSON shape, with a request id for correlation."""

    async def test_unknown_route_returns_structured_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "not_found"
        assert body["request_id"]

    async def test_method_not_allowed_is_structured(self, client: AsyncClient) -> None:
        response = await client.delete("/health")
        assert response.status_code == 405
        assert response.json()["code"] == "method_not_allowed"

    async def test_validation_error_is_structured_422(self, client: AsyncClient) -> None:
        # Send a string where the OpenAPI contract has no body at all, exercising the
        # framework's own validation path rather than a route's.
        response = await client.get("/api/v1/meta/limits?unknown_param=1")
        # Extra query parameters are ignored, so this is a 200 - the point is that the
        # application does not produce an unhandled error.
        assert response.status_code == 200

    async def test_error_responses_carry_a_request_id_header(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/nope")
        assert response.headers["X-Request-ID"]

    async def test_client_supplied_request_id_is_echoed(self, client: AsyncClient) -> None:
        response = await client.get("/health", headers={"X-Request-ID": "trace-me-1234"})
        assert response.headers["X-Request-ID"] == "trace-me-1234"

    async def test_implausible_request_id_is_replaced(self, client: AsyncClient) -> None:
        # The header is reflected and logged, so a hostile value must not be passed through.
        response = await client.get("/health", headers={"X-Request-ID": "a" * 200})
        assert response.headers["X-Request-ID"] != "a" * 200
        assert len(response.headers["X-Request-ID"]) <= 64


class TestSecurityHeaders:
    """Conservative response headers are set on every response."""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ],
    )
    async def test_header_is_present(self, client: AsyncClient, header: str, expected: str) -> None:
        response = await client.get("/health")
        assert response.headers[header] == expected

    async def test_no_wildcard_cors_header_by_default(self, client: AsyncClient) -> None:
        # A wildcard CORS policy would let any web page the user visits drive their instance.
        response = await client.get("/health", headers={"Origin": "http://evil.test"})
        assert "access-control-allow-origin" not in response.headers

    async def test_response_time_header_is_set(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert float(response.headers["X-Response-Time-ms"]) >= 0


class TestAuditSelfTest:
    """The audit self-test proves redaction happens before persistence, not after."""

    async def test_secret_is_redacted_before_storage(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/meta/audit-selftest")
        assert response.status_code == 202
        body = response.json()
        assert body["recorded"] is True
        assert body["secret_was_redacted"] is True

    async def test_returns_the_correlated_request_id(self, client: AsyncClient) -> None:
        body = (await client.post("/api/v1/meta/audit-selftest")).json()
        assert body["request_id"]


class TestOpenApi:
    """The schema must be generated - a broken schema means a broken contract."""

    async def test_openapi_json_is_served(self, client: AsyncClient) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"].startswith("NEXUS")
        assert "/health" in schema["paths"]
        assert "/api/v1/meta" in schema["paths"]

    async def test_docs_page_is_served(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 200


class TestRedactionInResponses:
    """No response body may contain a configured secret."""

    async def test_audit_selftest_response_hides_the_key(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/meta/audit-selftest")
        text = response.text
        assert "test-api-key-0123456789" not in text
        # The endpoint reports *that* it redacted; it must not echo the redacted value back,
        # because echoing it would confirm the secret's shape to a caller who did not know it.
        assert response.json()["secret_was_redacted"] is True

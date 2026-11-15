"""Proxy Manager API integration tests.

These drive the real router, the real service, the real database (temporary SQLite) and the real
background queue. The only thing replaced is the network: the checker is monkeypatched so no
socket is opened, which also means the tests never point NEXUS at a third-party service.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import CredentialCipher
from app.database.engine import get_session_factory
from app.jobs.queue import get_queue
from app.main import create_app
from app.models.core_models import AuditLog
from app.models.enums import ProxyStatus
from app.models.proxy_models import Proxy, ProxyCheck
from app.modules.proxy_manager.checker import CheckOutcome, ProxyHealthChecker
from tests.conftest import TEST_API_KEY

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def check_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every health check succeed, without touching the network."""

    async def _check(self, proxy):  # type: ignore[no-untyped-def]
        return CheckOutcome(ok=True, latency_ms=37, http_status=200)

    monkeypatch.setattr(ProxyHealthChecker, "check", _check)


@pytest.fixture
def check_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every health check fail with a connection error."""

    async def _check(self, proxy):  # type: ignore[no-untyped-def]
        return CheckOutcome(
            ok=False, latency_ms=11, error_class="connect_error", error_message="refused"
        )

    monkeypatch.setattr(ProxyHealthChecker, "check", _check)


async def _import(client: AsyncClient, content: str, **extra: object) -> dict:
    """POST a proxy list and return the parsed response, asserting it succeeded."""
    response = await client.post("/api/v1/proxy-sources", json={"content": content, **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def _list(client: AsyncClient, **params: object) -> dict:
    """GET the proxy list."""
    response = await client.get("/api/v1/proxies", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


class TestAuth:
    """Every proxy route requires the API key."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/proxies"),
            ("get", "/api/v1/proxies/stats"),
            ("get", "/api/v1/proxies/export"),
            ("get", "/api/v1/proxies/1"),
            ("post", "/api/v1/proxy-sources"),
            ("post", "/api/v1/proxies/1/check"),
            ("delete", "/api/v1/proxies/1"),
        ],
    )
    async def test_requires_api_key(
        self, anonymous_client: AsyncClient, method: str, path: str
    ) -> None:
        response = await getattr(anonymous_client, method)(path)
        assert response.status_code == 401


# --------------------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------------------


class TestImport:
    """Importing a list: parsing, counting and de-duplication."""

    async def test_imports_a_text_list(self, client: AsyncClient) -> None:
        body = await _import(client, "p1.example.test:8080\np2.example.test:3128\n")
        assert body["accepted"] == 2
        assert body["rejected"] == 0
        assert body["entries_seen"] == 2
        assert body["format_detected"] == "txt"
        assert body["source_id"] > 0

    async def test_imports_a_csv_list(self, client: AsyncClient) -> None:
        body = await _import(client, "host,port\np1.example.test,8080\n")
        assert body["accepted"] == 1
        assert body["format_detected"] == "csv"

    async def test_reports_rejections_with_line_numbers(self, client: AsyncClient) -> None:
        body = await _import(client, "p1.example.test:8080\nbroken-entry\n")
        assert body["accepted"] == 1
        assert body["rejected"] == 1
        assert body["rejected_samples"][0]["line_number"] == 2

    async def test_rejected_sample_does_not_leak_a_password(self, client: AsyncClient) -> None:
        body = await _import(client, "p.example.test:notaport:alice:supersecret\n")
        assert body["rejected"] == 1
        assert "supersecret" not in str(body)

    async def test_reimporting_the_same_list_counts_duplicates(self, client: AsyncClient) -> None:
        content = "p1.example.test:8080\n"
        first = await _import(client, content)
        second = await _import(client, content)
        assert first["accepted"] == 1
        assert second["accepted"] == 0
        assert second["duplicates"] == 1

    async def test_credentials_are_stored_encrypted(
        self, client: AsyncClient, test_settings
    ) -> None:
        body = await _import(client, "http://alice:supersecret@p1.example.test:8080\n")
        assert body["accepted"] == 1
        assert body["credentials_refused"] == 0
        # The response must never echo the password.
        assert "supersecret" not in str(body)

        async with get_session_factory()() as session:
            proxy = (await session.execute(select(Proxy))).scalar_one()
            assert proxy.username == "alice"
            assert proxy.password_encrypted is not None
            assert "supersecret" not in proxy.password_encrypted
            # ... and it really is recoverable with the configured key.
            cipher = CredentialCipher(test_settings.proxy_credential_key)
            assert cipher.decrypt(proxy.password_encrypted) == "supersecret"

    async def test_credentials_are_refused_when_no_key_is_configured(
        self, test_settings, db_engine: object
    ) -> None:
        # This is the mechanism behind "never store a proxy password in plain text".
        settings = test_settings.model_copy(deep=True)
        settings.proxy_credential_key = ""
        application = create_app(settings)

        transport = ASGITransport(app=application)
        async with AsyncClient(
            transport=transport, base_url="http://nexus.test", headers={"X-API-Key": TEST_API_KEY}
        ) as client:
            body = await _import(client, "http://alice:supersecret@p1.example.test:8080\n")

        assert body["accepted"] == 0
        assert body["credentials_refused"] == 1

        async with get_session_factory()() as session:
            assert (await session.execute(select(Proxy))).scalars().all() == []

    async def test_writes_an_audit_record(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\n")
        async with get_session_factory()() as session:
            records = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "proxy.import")))
                .scalars()
                .all()
            )
        assert len(records) == 1
        assert records[0].outcome == "success"
        assert records[0].detail_json["stored"] == 1

    async def test_empty_content_is_rejected_by_validation(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/proxy-sources", json={"content": ""})
        assert response.status_code == 422

    async def test_oversized_content_is_rejected(self, client: AsyncClient) -> None:
        # test_settings caps imports at 64 KiB.
        response = await client.post("/api/v1/proxy-sources", json={"content": "a:1\n" * 40_000})
        assert response.status_code == 422
        assert response.json()["code"] == "import_file_error"

    async def test_unknown_format_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/proxy-sources", json={"content": "p:80", "format": "xml"}
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------------------
# Listing, filtering, sorting
# --------------------------------------------------------------------------------------


class TestListing:
    """Read endpoints over a populated pool."""

    async def test_lists_with_pagination(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\np2.example.test:8081\np3.example.test:8082\n")
        body = await _list(client, limit=2, offset=0)
        assert body["meta"]["total"] == 3
        assert len(body["items"]) == 2
        assert body["meta"]["limit"] == 2

    async def test_filters_by_status(self, client: AsyncClient, check_ok) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]
        await client.post(f"/api/v1/proxies/{proxy_id}/check")

        healthy = await _list(client, status="healthy")
        assert healthy["meta"]["total"] == 1
        unknown = await _list(client, status="unknown")
        assert unknown["meta"]["total"] == 0

    async def test_search_matches_host_substring(self, client: AsyncClient) -> None:
        await _import(client, "alpha.example.test:8080\nbeta.example.test:8080\n")
        body = await _list(client, search="alpha")
        assert body["meta"]["total"] == 1
        assert body["items"][0]["host"] == "alpha.example.test"

    async def test_sort_by_latency(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        latencies = {"p1.example.test": 90, "p2.example.test": 10}

        async def _check(self, proxy):  # type: ignore[no-untyped-def]
            return CheckOutcome(ok=True, latency_ms=latencies[proxy.host], http_status=200)

        monkeypatch.setattr(ProxyHealthChecker, "check", _check)

        await _import(client, "p1.example.test:8080\np2.example.test:8080\n")
        for item in (await _list(client))["items"]:
            await client.post(f"/api/v1/proxies/{item['id']}/check")

        ascending = await _list(client, sort="latency_ms")
        assert [item["host"] for item in ascending["items"]] == [
            "p2.example.test",
            "p1.example.test",
        ]

        descending = await _list(client, sort="-latency_ms")
        assert [item["host"] for item in descending["items"]] == [
            "p1.example.test",
            "p2.example.test",
        ]

    async def test_unknown_sort_field_falls_back_instead_of_failing(
        self, client: AsyncClient
    ) -> None:
        await _import(client, "p1.example.test:8080\n")
        response = await client.get("/api/v1/proxies", params={"sort": "nonsense"})
        assert response.status_code == 200

    async def test_get_one(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]
        response = await client.get(f"/api/v1/proxies/{proxy_id}")
        assert response.status_code == 200
        assert response.json()["host"] == "p1.example.test"

    async def test_missing_proxy_is_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/proxies/999")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_response_never_contains_a_password_field(self, client: AsyncClient) -> None:
        await _import(client, "http://alice:supersecret@p1.example.test:8080\n")
        text = (await client.get("/api/v1/proxies")).text
        assert "password" not in text.lower()
        assert "supersecret" not in text


# --------------------------------------------------------------------------------------
# Mutation
# --------------------------------------------------------------------------------------


class TestMutation:
    """Enable/disable and delete."""

    async def test_disable_and_reenable(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]

        disabled = await client.patch(f"/api/v1/proxies/{proxy_id}", json={"is_enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["is_enabled"] is False

        enabled_only = await _list(client, enabled_only=True)
        assert enabled_only["meta"]["total"] == 0

        await client.patch(f"/api/v1/proxies/{proxy_id}", json={"is_enabled": True})
        assert (await _list(client, enabled_only=True))["meta"]["total"] == 1

    async def test_delete_removes_the_proxy(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]

        response = await client.delete(f"/api/v1/proxies/{proxy_id}")
        assert response.status_code == 204
        assert (await client.get(f"/api/v1/proxies/{proxy_id}")).status_code == 404

    async def test_delete_cascades_to_check_history(self, client: AsyncClient, check_ok) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]
        await client.post(f"/api/v1/proxies/{proxy_id}/check")

        async with get_session_factory()() as session:
            assert (await session.execute(select(ProxyCheck))).scalars().all()

        await client.delete(f"/api/v1/proxies/{proxy_id}")

        async with get_session_factory()() as session:
            assert (await session.execute(select(ProxyCheck))).scalars().all() == []

    async def test_patch_missing_proxy_is_404(self, client: AsyncClient) -> None:
        response = await client.patch("/api/v1/proxies/999", json={"is_enabled": False})
        assert response.status_code == 404


# --------------------------------------------------------------------------------------
# Health checks
# --------------------------------------------------------------------------------------


class TestChecks:
    """Single and batch health checks."""

    async def test_successful_check_updates_state_and_history(
        self, client: AsyncClient, check_ok
    ) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]

        response = await client.post(f"/api/v1/proxies/{proxy_id}/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["status"] == ProxyStatus.HEALTHY.value
        assert body["latency_ms"] == 37

        async with get_session_factory()() as session:
            proxy = await session.get(Proxy, proxy_id)
            assert proxy.status == ProxyStatus.HEALTHY.value
            assert proxy.latency_ms == 37
            assert proxy.success_count >= 1
            checks = (
                (await session.execute(select(ProxyCheck).where(ProxyCheck.proxy_id == proxy_id)))
                .scalars()
                .all()
            )
            assert len(checks) == 1
            assert checks[0].ok is True

    async def test_failed_check_then_expiry(self, client: AsyncClient, check_fail) -> None:  # type: ignore[no-untyped-def]
        # test_settings sets expire_after_failures=2, so two failures mark the proxy expired.
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]

        first = (await client.post(f"/api/v1/proxies/{proxy_id}/check")).json()
        assert first["ok"] is False
        assert first["status"] == ProxyStatus.UNHEALTHY.value
        assert first["error_class"] == "connect_error"

        second = (await client.post(f"/api/v1/proxies/{proxy_id}/check")).json()
        assert second["status"] == ProxyStatus.EXPIRED.value

    async def test_a_success_resets_the_failure_streak(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outcomes = [
            CheckOutcome(ok=False, error_class="connect_error"),
            CheckOutcome(ok=True, latency_ms=5),
        ]

        async def _check(self, proxy):  # type: ignore[no-untyped-def]
            return outcomes.pop(0)

        monkeypatch.setattr(ProxyHealthChecker, "check", _check)

        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]
        await client.post(f"/api/v1/proxies/{proxy_id}/check")
        await client.post(f"/api/v1/proxies/{proxy_id}/check")

        async with get_session_factory()() as session:
            proxy = await session.get(Proxy, proxy_id)
            assert proxy.consecutive_failures == 0
            assert proxy.status == ProxyStatus.HEALTHY.value

    async def test_check_writes_an_audit_record(self, client: AsyncClient, check_ok) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\n")
        proxy_id = (await _list(client))["items"][0]["id"]
        await client.post(f"/api/v1/proxies/{proxy_id}/check")

        async with get_session_factory()() as session:
            records = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "proxy.check")))
                .scalars()
                .all()
            )
        assert len(records) == 1

    async def test_check_missing_proxy_is_404(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/proxies/999/check")).status_code == 404


# --------------------------------------------------------------------------------------
# Batch check job
# --------------------------------------------------------------------------------------


class TestBatchCheck:
    """The batch endpoint enqueues work and the job performs it."""

    async def test_batch_check_runs_and_updates_proxies(
        self, client: AsyncClient, check_ok, test_settings
    ) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\np2.example.test:8080\n")
        ids = [item["id"] for item in (await _list(client))["items"]]

        response = await client.post("/api/v1/proxies/check-batch", json={"proxy_ids": ids})
        assert response.status_code == 202
        body = response.json()
        assert body["proxy_count"] == 2
        job_id = body["job_id"]

        queue = get_queue(test_settings)
        for _ in range(100):
            status = await queue.get_status(job_id)
            if status.status in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.02)
        assert status.status == "completed", status

        async with get_session_factory()() as session:
            proxies = (await session.execute(select(Proxy))).scalars().all()
        assert all(proxy.status == ProxyStatus.HEALTHY.value for proxy in proxies)

    async def test_batch_with_neither_ids_nor_filter_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/proxies/check-batch", json={})
        assert response.status_code == 422

    async def test_batch_with_unknown_ids_is_404(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/proxies/check-batch", json={"proxy_ids": [999]})
        assert response.status_code == 404

    async def test_batch_over_the_limit_is_rejected(
        self, client: AsyncClient, test_settings
    ) -> None:
        # test_settings sets batch_limit=50; 51 ids must fail before any work starts.
        ids = list(range(1, test_settings.proxy.batch_limit + 2))
        response = await client.post("/api/v1/proxies/check-batch", json={"proxy_ids": ids})
        assert response.status_code == 422

    async def test_status_filter_selects_proxies(
        self, client: AsyncClient, check_ok, test_settings
    ) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\n")

        response = await client.post(
            "/api/v1/proxies/check-batch", json={"status_filter": "unknown"}
        )
        assert response.status_code == 202
        assert response.json()["proxy_count"] == 1


# --------------------------------------------------------------------------------------
# Stats and export
# --------------------------------------------------------------------------------------


class TestStats:
    """Dashboard counts."""

    async def test_counts_by_status(self, client: AsyncClient, check_ok) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\np2.example.test:8080\n")
        ids = [item["id"] for item in (await _list(client))["items"]]
        await client.post(f"/api/v1/proxies/{ids[0]}/check")

        body = (await client.get("/api/v1/proxies/stats")).json()
        assert body["total"] == 2
        assert body["by_status"]["healthy"] == 1
        assert body["by_status"]["unknown"] == 1
        assert body["healthy_with_latency"] == 1
        assert body["average_latency_ms"] == 37.0

    async def test_average_is_null_with_nothing_measured(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\n")
        body = (await client.get("/api/v1/proxies/stats")).json()
        assert body["average_latency_ms"] is None


class TestExport:
    """Exports carry no credentials."""

    async def test_csv_export(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\n")
        response = await client.get("/api/v1/proxies/export", params={"format": "csv"})
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        lines = response.text.strip().splitlines()
        assert lines[0].startswith("protocol,host,port")
        assert "p1.example.test" in lines[1]

    async def test_json_export(self, client: AsyncClient) -> None:
        await _import(client, "p1.example.test:8080\n")
        response = await client.get("/api/v1/proxies/export", params={"format": "json"})
        assert response.status_code == 200
        assert response.json()[0]["host"] == "p1.example.test"

    async def test_export_hides_credentials(self, client: AsyncClient) -> None:
        await _import(client, "http://alice:supersecret@p1.example.test:8080\n")
        response = await client.get("/api/v1/proxies/export", params={"format": "csv"})
        assert "supersecret" not in response.text
        assert "alice" in response.text  # the username is not a secret

    async def test_unsupported_export_format_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/proxies/export", params={"format": "xml"})
        assert response.status_code == 422

    async def test_export_filters_apply(self, client: AsyncClient, check_ok) -> None:  # type: ignore[no-untyped-def]
        await _import(client, "p1.example.test:8080\np2.example.test:8080\n")
        ids = [item["id"] for item in (await _list(client))["items"]]
        await client.post(f"/api/v1/proxies/{ids[0]}/check")

        response = await client.get(
            "/api/v1/proxies/export", params={"format": "json", "status": "healthy"}
        )
        assert len(response.json()) == 1
        assert response.json()[0]["status"] == "healthy"

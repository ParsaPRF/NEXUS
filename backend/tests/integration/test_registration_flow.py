"""Registration Lab end-to-end tests.

These run the **whole loop** in-process:

    NEXUS API -> campaign job -> adapter -> demo target -> its mailer -> NEXUS's mail backend
              -> inbox poll -> code extraction -> verify -> account status

Only the network is replaced (the demo application is driven through an injected transport), so
the test exercises the real service, executor, analyzer, adapter, inbox and database. That is what
makes "email verification works end to end" a claim backed by an execution rather than a hope.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from demo_target.app import CallbackMailer, create_demo_app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database.engine import get_session_factory
from app.jobs.queue import get_queue
from app.models.enums import CampaignStatus
from app.models.registration_models import RegistrationResult
from app.modules.email_manager.backends import InMemoryMailBackend, get_backend
from app.modules.registration_lab.adapters import set_test_transport

DEMO_BASE_URL = "http://127.0.0.1:9000"


@pytest.fixture(autouse=True)
def _reset_transport():
    """Never leave a transport installed for another test."""
    set_test_transport(None)
    yield
    set_test_transport(None)


@pytest.fixture
def demo(test_settings) -> object:  # type: ignore[no-untyped-def]
    """Run the demo app in-process, delivering its mail into NEXUS's in-memory backend."""
    backend = get_backend(test_settings)
    assert isinstance(backend, InMemoryMailBackend)

    def _deliver(to: str, subject: str, text: str) -> None:
        backend.deliver(recipient=to, sender="no-reply@demo.test", subject=subject, body_text=text)

    app = create_demo_app(mailer=CallbackMailer(_deliver))
    set_test_transport(ASGITransport(app=app))
    return app


async def _create_target(client: AsyncClient, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "name": "Bundled demo application",
        "base_url": DEMO_BASE_URL,
        "kind": "demo",
        "adapter_name": "demo_api",
        "is_authorized": True,
        "authorization_note": "Bundled demonstration application owned by this repository.",
    }
    payload.update(overrides)
    response = await client.post("/api/v1/targets", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _run_campaign(client: AsyncClient, target_id: int, **overrides: object) -> dict:
    """Create a campaign and wait for its job to finish."""
    payload: dict[str, object] = {
        "name": "smoke campaign",
        "target_id": target_id,
        "count": 1,
        "concurrency": 1,
        "rate_limit_per_minute": 120,
        "email_timeout_seconds": 5,
    }
    payload.update(overrides)
    response = await client.post("/api/v1/campaigns", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _wait_for_job(settings, job_id: str, limit_seconds: float = 20.0) -> str:  # type: ignore[no-untyped-def]
    """Poll the in-process queue until the job finishes."""
    queue = get_queue(settings)
    deadline = asyncio.get_event_loop().time() + limit_seconds
    while asyncio.get_event_loop().time() < deadline:
        status = await queue.get_status(job_id)
        if status.status in {"completed", "failed", "cancelled"}:
            return status.status
        await asyncio.sleep(0.05)
    msg = f"job {job_id} did not finish in {limit_seconds}s"
    raise AssertionError(msg)


# --------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------


class TestHappyPath:
    """Registration completes end to end."""

    async def test_single_identity_completes(
        self, client: AsyncClient, test_settings, demo
    ) -> None:
        target = await _create_target(client)
        body = await _run_campaign(client, target["id"], count=1)
        assert await _wait_for_job(test_settings, body["job_id"]) == "completed"

        results = (
            await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}/results")
        ).json()
        assert results["campaign"]["status"] == CampaignStatus.COMPLETED.value
        assert results["summary"]["success"] == 1
        assert results["items"][0]["outcome"] == "success"
        assert results["items"][0]["status"] == "active"
        assert results["items"][0]["verification_code"]

    async def test_account_is_really_active_in_the_target(
        self, client: AsyncClient, test_settings, demo
    ) -> None:
        # The whole point: the demo target must report an active account, not just a green row.
        target = await _create_target(client)
        body = await _run_campaign(client, target["id"], count=1)
        await _wait_for_job(test_settings, body["job_id"])

        results = (
            await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}/results")
        ).json()
        email = results["items"][0]["email"]
        assert results["items"][0]["account_status"] == "active"

        demo_client = httpx.AsyncClient(
            transport=ASGITransport(app=demo),
            base_url="http://demo.test",  # type: ignore[arg-type]
        )
        async with demo_client:
            status = (await demo_client.get(f"/api/status/{email}")).json()
        assert status["status"] == "active"

    async def test_multiple_identities(self, client: AsyncClient, test_settings, demo) -> None:
        target = await _create_target(client)
        body = await _run_campaign(client, target["id"], count=3, concurrency=2)
        await _wait_for_job(test_settings, body["job_id"])

        results = (
            await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}/results")
        ).json()
        assert results["summary"]["success"] == 3
        assert results["campaign"]["completed_tests"] == 3
        assert results["campaign"]["succeeded_tests"] == 3

    async def test_results_are_persisted_with_steps(
        self, client: AsyncClient, test_settings, demo
    ) -> None:
        target = await _create_target(client)
        body = await _run_campaign(client, target["id"], count=1)
        await _wait_for_job(test_settings, body["job_id"])

        async with get_session_factory()() as session:
            rows = (await session.execute(select(RegistrationResult))).scalars().all()
        assert len(rows) == 1
        assert rows[0].outcome == "success"
        assert rows[0].steps_json["submit"] == "accepted"
        assert rows[0].duration_ms is not None


# --------------------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------------------


class TestControls:
    """A control from the target stops the campaign and is recorded as such."""

    @pytest.mark.parametrize(
        ("mode", "expected_outcome"),
        [
            ("rate_limit", "rate_limited"),
            ("captcha", "captcha_encountered"),
            ("access_denied", "access_denied"),
        ],
    )
    async def test_control_stops_the_campaign(
        self, client: AsyncClient, test_settings, demo, mode: str, expected_outcome: str
    ) -> None:
        target = await _create_target(client)

        # Put the target into the control mode before the campaign runs.
        async with httpx.AsyncClient(
            transport=ASGITransport(app=demo),
            base_url="http://demo.test",  # type: ignore[arg-type]
        ) as demo_client:
            await demo_client.post("/api/_simulate", json={"mode": mode})

        body = await _run_campaign(client, target["id"], count=3)
        await _wait_for_job(test_settings, body["job_id"])

        results = (
            await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}/results")
        ).json()
        assert results["campaign"]["status"] == CampaignStatus.STOPPED_BY_TARGET.value
        assert results["summary"]["controls_encountered"] >= 1
        assert results["items"][0]["outcome"] == expected_outcome
        assert results["campaign"]["stop_reason"]

    async def test_control_is_not_retried(self, client: AsyncClient, test_settings, demo) -> None:
        # One control stops the run: it must not process the remaining identities.
        target = await _create_target(client)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=demo),
            base_url="http://demo.test",  # type: ignore[arg-type]
        ) as demo_client:
            await demo_client.post("/api/_simulate", json={"mode": "rate_limit"})

        body = await _run_campaign(client, target["id"], count=4, concurrency=1)
        await _wait_for_job(test_settings, body["job_id"])

        results = (
            await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}/results")
        ).json()
        assert results["campaign"]["completed_tests"] <= 1
        assert results["summary"]["controls_encountered"] == 1


# --------------------------------------------------------------------------------------
# Authorization gates
# --------------------------------------------------------------------------------------


class TestAuthorization:
    """Nothing runs against an unauthorized or unlisted target."""

    async def test_unauthorized_target_cannot_be_campaigned(
        self, client: AsyncClient, demo
    ) -> None:
        target = await _create_target(client, is_authorized=False, authorization_note=None)
        response = await client.post(
            "/api/v1/campaigns",
            json={"name": "blocked", "target_id": target["id"], "count": 1},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "target_not_authorized"

    async def test_authorizing_with_a_note_unblocks_it(
        self, client: AsyncClient, test_settings, demo
    ) -> None:
        target = await _create_target(client, is_authorized=False, authorization_note=None)
        authorized = await client.post(
            f"/api/v1/targets/{target['id']}/authorize",
            params={"note": "QA lead approved this demo run on 2026-11-15."},
        )
        assert authorized.status_code == 200
        assert authorized.json()["may_be_tested"] is True

        body = await _run_campaign(client, target["id"], count=1)
        await _wait_for_job(test_settings, body["job_id"])
        results = (
            await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}/results")
        ).json()
        assert results["summary"]["success"] == 1

    async def test_authorize_without_a_note_is_rejected(self, client: AsyncClient, demo) -> None:
        target = await _create_target(client, is_authorized=False, authorization_note=None)
        response = await client.post(
            f"/api/v1/targets/{target['id']}/authorize", params={"note": ""}
        )
        assert response.status_code == 422

    async def test_external_target_needs_allowlisting(self, client: AsyncClient) -> None:
        # With an empty allowlist, an external target cannot even be registered.
        response = await client.post(
            "/api/v1/targets",
            json={
                "name": "staging",
                "base_url": "https://app.example.test",
                "kind": "api",
                "adapter_name": "demo_api",
                "is_authorized": True,
                "authorization_note": "not really",
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "target_not_authorized"

    async def test_demo_target_must_be_local(self, client: AsyncClient) -> None:
        # "kind: demo" is not an allowlist bypass: a demo target must be on loopback.
        response = await client.post(
            "/api/v1/targets",
            json={
                "name": "fake demo",
                "base_url": "https://93.184.216.34",
                "kind": "demo",
                "adapter_name": "demo_api",
                "is_authorized": True,
                "authorization_note": "attempting a bypass",
            },
        )
        assert response.status_code == 403

    async def test_duplicate_target_is_conflict(self, client: AsyncClient, demo) -> None:
        await _create_target(client)
        response = await client.post(
            "/api/v1/targets",
            json={
                "name": "duplicate",
                "base_url": DEMO_BASE_URL,
                "kind": "demo",
                "adapter_name": "demo_api",
            },
        )
        assert response.status_code == 409

    async def test_unknown_target_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/targets/999")).status_code == 404

    async def test_unauthorized_target_is_flagged_in_the_list(
        self, client: AsyncClient, demo
    ) -> None:
        await _create_target(client, is_authorized=False, authorization_note=None)
        targets = (await client.get("/api/v1/targets")).json()
        assert targets[0]["may_be_tested"] is False


# --------------------------------------------------------------------------------------
# Identities
# --------------------------------------------------------------------------------------


class TestIdentities:
    """Generated versus imported identities."""

    async def test_imported_identity_must_be_on_a_hosted_domain(
        self, client: AsyncClient, demo
    ) -> None:
        # An address we cannot receive mail for would fail later as a misleading timeout.
        target = await _create_target(client)
        response = await client.post(
            "/api/v1/campaigns",
            json={
                "name": "foreign",
                "target_id": target["id"],
                "identities_text": "someone@gmail.com",
            },
        )
        assert response.status_code == 422
        assert "unsupported_domains" in response.json()["details"]

    async def test_imported_identity_on_our_domain_works(
        self, client: AsyncClient, test_settings, demo
    ) -> None:
        target = await _create_target(client)
        inbox = (await client.post("/api/v1/inboxes", json={"local_part": "imported"})).json()

        body = await _run_campaign(client, target["id"], count=0, identities_text=inbox["address"])
        await _wait_for_job(test_settings, body["job_id"])

        results = (
            await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}/results")
        ).json()
        assert results["items"][0]["email"] == inbox["address"]
        assert results["summary"]["success"] == 1

    async def test_no_identities_is_rejected(self, client: AsyncClient, demo) -> None:
        target = await _create_target(client)
        response = await client.post(
            "/api/v1/campaigns", json={"name": "empty", "target_id": target["id"]}
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


class TestLifecycle:
    """Campaign state transitions."""

    async def test_cancel_a_queued_campaign(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, demo
    ) -> None:
        # Stop the job from running so the campaign stays queued and cancellation is deterministic.
        from app.jobs.queue import InProcessJobQueue

        async def _noop(self, job_type, payload=None, **options):  # type: ignore[no-untyped-def]
            return "unstarted-job"

        monkeypatch.setattr(InProcessJobQueue, "enqueue", _noop)

        target = await _create_target(client)
        body = await _run_campaign(client, target["id"], count=1)
        public_id = body["campaign"]["public_id"]

        response = await client.post(f"/api/v1/campaigns/{public_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == CampaignStatus.CANCELLED.value
        assert response.json()["stop_reason"] == "cancelled by operator"

    async def test_cancel_after_completion_is_conflict(
        self, client: AsyncClient, test_settings, demo
    ) -> None:
        target = await _create_target(client)
        body = await _run_campaign(client, target["id"], count=1)
        await _wait_for_job(test_settings, body["job_id"])

        response = await client.post(f"/api/v1/campaigns/{body['campaign']['public_id']}/cancel")
        assert response.status_code == 409

    async def test_campaign_list_and_lookup(self, client: AsyncClient, test_settings, demo) -> None:
        target = await _create_target(client)
        body = await _run_campaign(client, target["id"], count=1)
        await _wait_for_job(test_settings, body["job_id"])

        listing = (await client.get("/api/v1/campaigns")).json()
        assert listing["meta"]["total"] == 1

        one = (await client.get(f"/api/v1/campaigns/{body['campaign']['public_id']}")).json()
        assert one["total_tests"] == 1
        assert one["progress_percent"] == 100.0

    async def test_unknown_campaign_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/campaigns/nope")).status_code == 404


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


class TestAuth:
    """Campaign routes require the API key."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/targets"),
            ("post", "/api/v1/targets"),
            ("get", "/api/v1/campaigns"),
            ("post", "/api/v1/campaigns"),
        ],
    )
    async def test_requires_api_key(
        self, anonymous_client: AsyncClient, method: str, path: str
    ) -> None:
        assert (await getattr(anonymous_client, method)(path)).status_code == 401

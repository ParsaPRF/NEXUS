"""Email Manager API integration tests.

Drives the real router, service, database and the in-memory mail backend. Messages are injected
through ``InMemoryMailBackend.deliver`` - which simulates "the application under test sent a
verification email" without any SMTP, network or third-party involvement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.database.engine import get_session_factory
from app.main import create_app
from app.models.core_models import AuditLog
from app.models.email_models import Inbox, Message
from app.modules.email_manager.backends import InMemoryMailBackend, get_backend
from tests.conftest import TEST_API_KEY


def _backend(settings) -> InMemoryMailBackend:  # type: ignore[no-untyped-def]
    backend = get_backend(settings)
    assert isinstance(backend, InMemoryMailBackend)
    return backend


async def _create_inbox(client: AsyncClient, **payload: object) -> dict:
    response = await client.post("/api/v1/inboxes", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _deliver_and_poll(
    client: AsyncClient,
    settings,  # type: ignore[no-untyped-def]
    inbox: dict,
    *,
    body_text: str = "Your verification code is 481920",
    subject: str = "Your verification code",
    sender: str = "no-reply@demo.test",
) -> dict:
    """Simulate an application sending a verification email, then poll for it."""
    _backend(settings).deliver(
        recipient=inbox["address"], sender=sender, subject=subject, body_text=body_text
    )
    response = await client.post(f"/api/v1/inboxes/{inbox['public_id']}/poll")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


class TestAuth:
    """Inbox routes require the API key."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/inboxes"),
            ("get", "/api/v1/email-domains"),
            ("post", "/api/v1/inboxes"),
            ("post", "/api/v1/inboxes/bulk"),
            ("post", "/api/v1/inboxes/cleanup"),
            ("get", "/api/v1/messages/1"),
        ],
    )
    async def test_requires_api_key(
        self, anonymous_client: AsyncClient, method: str, path: str
    ) -> None:
        assert (await getattr(anonymous_client, method)(path)).status_code == 401


# --------------------------------------------------------------------------------------
# Domains
# --------------------------------------------------------------------------------------


class TestDomains:
    """Only reserved or owner-verified domains host inboxes."""

    async def test_reserved_domain_can_be_registered(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/email-domains", json={"domain": "example.invalid"})
        assert response.status_code == 201
        body = response.json()
        assert body["is_reserved"] is True
        assert body["owner_verified"] is False

    async def test_non_reserved_domain_requires_ownership_confirmation(
        self, client: AsyncClient
    ) -> None:
        # The mechanism behind "no third-party mailboxes": you must assert you control it.
        response = await client.post("/api/v1/email-domains", json={"domain": "real.example.org"})
        assert response.status_code == 403
        assert response.json()["code"] == "email_domain_not_allowed"

    async def test_owner_verified_domain_is_accepted(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/email-domains",
            json={"domain": "staging.example.org", "owner_verified": True},
        )
        assert response.status_code == 201
        assert response.json()["owner_verified"] is True

    async def test_duplicate_domain_is_a_conflict(self, client: AsyncClient) -> None:
        await client.post("/api/v1/email-domains", json={"domain": "example.invalid"})
        response = await client.post("/api/v1/email-domains", json={"domain": "example.invalid"})
        assert response.status_code == 409

    async def test_malformed_domain_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/email-domains", json={"domain": "notadomain"})
        assert response.status_code == 422

    async def test_list_is_empty_before_any_inbox(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/email-domains")).json() == []

    async def test_default_domain_is_created_on_first_inbox(self, client: AsyncClient) -> None:
        await _create_inbox(client)
        domains = (await client.get("/api/v1/email-domains")).json()
        assert len(domains) == 1
        assert domains[0]["domain"] == "example.test"
        assert domains[0]["is_reserved"] is True
        assert domains[0]["inbox_count"] == 1

    async def test_registration_is_audited(self, client: AsyncClient) -> None:
        await client.post("/api/v1/email-domains", json={"domain": "example.invalid"})
        async with get_session_factory()() as session:
            records = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "domain.create")))
                .scalars()
                .all()
            )
        assert len(records) == 1


# --------------------------------------------------------------------------------------
# Inbox creation
# --------------------------------------------------------------------------------------


class TestInboxCreation:
    """Single and bulk creation."""

    async def test_creates_an_inbox_on_the_reserved_domain(self, client: AsyncClient) -> None:
        inbox = await _create_inbox(client, label="manual-qa")
        assert inbox["address"].endswith("@example.test")
        assert inbox["status"] == "active"
        assert inbox["label"] == "manual-qa"
        assert inbox["message_count"] == 0
        assert inbox["expires_at"] is not None

    async def test_aliases_are_unique(self, client: AsyncClient) -> None:
        first = await _create_inbox(client)
        second = await _create_inbox(client)
        assert first["address"] != second["address"]

    async def test_all_inboxes_are_on_the_reserved_domain(self, client: AsyncClient) -> None:
        # This is the property that makes the module safe: mail can never leave the machine.
        for _ in range(3):
            inbox = await _create_inbox(client)
            assert inbox["address"].endswith("@example.test")

    async def test_requested_alias_is_used(self, client: AsyncClient) -> None:
        inbox = await _create_inbox(client, local_part="fixed-alias")
        assert inbox["address"] == "fixed-alias@example.test"

    async def test_duplicate_alias_is_a_conflict(self, client: AsyncClient) -> None:
        await _create_inbox(client, local_part="taken")
        response = await client.post("/api/v1/inboxes", json={"local_part": "taken"})
        assert response.status_code == 409

    @pytest.mark.parametrize("alias", ["bad alias", "UPPER!", ".leading", "trailing.", "a..b"])
    async def test_invalid_alias_is_rejected(self, client: AsyncClient, alias: str) -> None:
        response = await client.post("/api/v1/inboxes", json={"local_part": alias})
        assert response.status_code == 422

    async def test_ttl_controls_expiry(self, client: AsyncClient) -> None:
        inbox = await _create_inbox(client, ttl_hours=2)
        expires = datetime.fromisoformat(inbox["expires_at"].replace("Z", "+00:00"))
        delta = expires - datetime.now(UTC)
        assert timedelta(hours=1, minutes=30) < delta < timedelta(hours=2, minutes=5)

    async def test_bulk_creation(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/inboxes/bulk", json={"count": 3, "label_prefix": "campaign"}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["created"] == 3
        assert len(body["inboxes"]) == 3
        assert len({item["address"] for item in body["inboxes"]}) == 3
        assert body["inboxes"][0]["label"] == "campaign-1"

    async def test_bulk_over_the_limit_is_rejected(self, client: AsyncClient) -> None:
        # Default cap is 100. Bulk creation is for test setup, not for generating mailboxes
        # at scale.
        response = await client.post("/api/v1/inboxes/bulk", json={"count": 101})
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    async def test_creation_is_audited(self, client: AsyncClient) -> None:
        await _create_inbox(client)
        async with get_session_factory()() as session:
            records = (
                (await session.execute(select(AuditLog).where(AuditLog.action == "inbox.create")))
                .scalars()
                .all()
            )
        assert len(records) == 1


# --------------------------------------------------------------------------------------
# Listing and lookup
# --------------------------------------------------------------------------------------


class TestListing:
    """Inbox list, filters and lookup."""

    async def test_lists_with_pagination(self, client: AsyncClient) -> None:
        for _ in range(3):
            await _create_inbox(client)
        body = (await client.get("/api/v1/inboxes", params={"limit": 2})).json()
        assert body["meta"]["total"] == 3
        assert len(body["items"]) == 2

    async def test_filters_by_status(self, client: AsyncClient) -> None:
        await _create_inbox(client)
        active = (await client.get("/api/v1/inboxes", params={"status": "active"})).json()
        expired = (await client.get("/api/v1/inboxes", params={"status": "expired"})).json()
        assert active["meta"]["total"] == 1
        assert expired["meta"]["total"] == 0

    async def test_search_matches_address(self, client: AsyncClient) -> None:
        await _create_inbox(client, local_part="findme")
        await _create_inbox(client)
        body = (await client.get("/api/v1/inboxes", params={"search": "findme"})).json()
        assert body["meta"]["total"] == 1

    async def test_get_one(self, client: AsyncClient) -> None:
        inbox = await _create_inbox(client)
        response = await client.get(f"/api/v1/inboxes/{inbox['public_id']}")
        assert response.status_code == 200
        assert response.json()["address"] == inbox["address"]

    async def test_unknown_inbox_is_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/inboxes/does-not-exist")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_delete_removes_the_inbox(self, client: AsyncClient) -> None:
        inbox = await _create_inbox(client)
        assert (await client.delete(f"/api/v1/inboxes/{inbox['public_id']}")).status_code == 204
        assert (await client.get(f"/api/v1/inboxes/{inbox['public_id']}")).status_code == 404


# --------------------------------------------------------------------------------------
# Message ingestion
# --------------------------------------------------------------------------------------


class TestIngestion:
    """Polling, idempotency, extraction and message retrieval."""

    async def test_poll_ingests_a_message(self, client: AsyncClient, test_settings) -> None:
        inbox = await _create_inbox(client)
        result = await _deliver_and_poll(client, test_settings, inbox)

        assert result["ingested"] == 1
        assert result["skipped"] == 0

        refreshed = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}")).json()
        assert refreshed["message_count"] == 1
        assert refreshed["last_message_at"] is not None

    async def test_polling_twice_is_idempotent(self, client: AsyncClient, test_settings) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        second = await client.post(f"/api/v1/inboxes/{inbox['public_id']}/poll")
        body = second.json()
        assert body["ingested"] == 0
        assert body["already_known"] >= 1

        messages = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}/messages")).json()
        assert messages["meta"]["total"] == 1

    async def test_message_carries_the_extracted_code(
        self, client: AsyncClient, test_settings
    ) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        messages = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}/messages")).json()
        item = messages["items"][0]
        assert item["extracted_code"] == "481920"
        assert item["extraction_confidence"] >= 0.7
        assert item["subject"] == "Your verification code"
        assert item["sender"] == "no-reply@demo.test"

    async def test_message_detail_includes_bodies(self, client: AsyncClient, test_settings) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        messages = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}/messages")).json()
        detail = (await client.get(f"/api/v1/messages/{messages['items'][0]['id']}")).json()
        assert "481920" in detail["body_text"]

    async def test_message_from_another_recipient_is_refused(
        self, client: AsyncClient, test_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defence in depth: even if the backend hands us someone else's message, it is not stored.
        inbox = await _create_inbox(client)
        backend = _backend(test_settings)
        foreign_id = backend.deliver(
            recipient="someone-else@example.test", body_text="Your code is 999999"
        )

        async def fake_list_ids(recipient: str, *, limit: int = 200) -> list[str]:
            return [foreign_id]

        monkeypatch.setattr(backend, "list_ids", fake_list_ids)

        result = (await client.post(f"/api/v1/inboxes/{inbox['public_id']}/poll")).json()
        assert result["ingested"] == 0
        assert result["skipped"] == 1

        messages = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}/messages")).json()
        assert messages["meta"]["total"] == 0

    async def test_re_extraction_endpoint(self, client: AsyncClient, test_settings) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        messages = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}/messages")).json()
        message_id = messages["items"][0]["id"]

        response = await client.post(f"/api/v1/messages/{message_id}/extract-code")
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == "481920"
        assert body["candidates"]
        assert body["strategy"] is not None

    async def test_poll_unknown_inbox_is_404(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/inboxes/nope/poll")).status_code == 404

    async def test_messages_of_unknown_inbox_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/inboxes/nope/messages")).status_code == 404

    async def test_unknown_message_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/messages/999")).status_code == 404

    async def test_message_search(self, client: AsyncClient, test_settings) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox, subject="Password reset")
        await _deliver_and_poll(client, test_settings, inbox, subject="Welcome aboard")

        found = (
            await client.get(
                f"/api/v1/inboxes/{inbox['public_id']}/messages", params={"search": "password"}
            )
        ).json()
        assert found["meta"]["total"] == 1
        assert found["items"][0]["subject"] == "Password reset"

    async def test_deleting_an_inbox_cascades_to_messages(
        self, client: AsyncClient, test_settings
    ) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        await client.delete(f"/api/v1/inboxes/{inbox['public_id']}")

        async with get_session_factory()() as session:
            remaining = (
                await session.execute(select(func.count()).select_from(Message))
            ).scalar_one()
        assert remaining == 0


# --------------------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------------------


class TestExpiry:
    """The expiry sweep."""

    async def test_cleanup_expires_overdue_inboxes(
        self, client: AsyncClient, test_settings
    ) -> None:
        inbox = await _create_inbox(client, ttl_hours=1)

        # Move the deadline into the past rather than waiting an hour.
        async with get_session_factory()() as session:
            row = (
                await session.execute(select(Inbox).where(Inbox.public_id == inbox["public_id"]))
            ).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
            await session.commit()

        response = await client.post("/api/v1/inboxes/cleanup")
        assert response.status_code == 200
        assert response.json()["expired"] == 1

        refreshed = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}")).json()
        assert refreshed["status"] == "expired"

    async def test_cleanup_leaves_valid_inboxes_alone(
        self, client: AsyncClient, test_settings
    ) -> None:
        await _create_inbox(client, ttl_hours=24)
        response = await client.post("/api/v1/inboxes/cleanup")
        assert response.json()["expired"] == 0

    async def test_expiry_keeps_messages(self, client: AsyncClient, test_settings) -> None:
        # Expiry is a status change, not a deletion: the data stays inspectable until the
        # operator deletes the inbox.
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        async with get_session_factory()() as session:
            row = (
                await session.execute(select(Inbox).where(Inbox.public_id == inbox["public_id"]))
            ).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()

        await client.post("/api/v1/inboxes/cleanup")

        messages = (await client.get(f"/api/v1/inboxes/{inbox['public_id']}/messages")).json()
        assert messages["meta"]["total"] == 1


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------


class TestExport:
    """Message export."""

    async def test_csv_export(self, client: AsyncClient, test_settings) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        response = await client.get(
            f"/api/v1/inboxes/{inbox['public_id']}/export", params={"format": "csv"}
        )
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "481920" in response.text
        assert response.text.splitlines()[0].startswith("id,sender,recipient")

    async def test_json_export(self, client: AsyncClient, test_settings) -> None:
        inbox = await _create_inbox(client)
        await _deliver_and_poll(client, test_settings, inbox)

        response = await client.get(
            f"/api/v1/inboxes/{inbox['public_id']}/export", params={"format": "json"}
        )
        body = response.json()
        assert len(body) == 1
        assert body[0]["extracted_code"] == "481920"

    async def test_unsupported_export_format_is_rejected(self, client: AsyncClient) -> None:
        inbox = await _create_inbox(client)
        response = await client.get(
            f"/api/v1/inboxes/{inbox['public_id']}/export", params={"format": "xml"}
        )
        assert response.status_code == 422

    async def test_export_of_unknown_inbox_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/inboxes/nope/export")).status_code == 404


# --------------------------------------------------------------------------------------
# Domain safety
# --------------------------------------------------------------------------------------


class TestDomainSafety:
    """A configured non-reserved domain must not silently host inboxes."""

    async def test_non_reserved_configured_domain_is_refused(
        self, test_settings, db_engine: object
    ) -> None:
        # If the operator points the default domain at a real domain without verifying ownership,
        # inbox creation fails closed instead of quietly hosting test addresses there.
        settings = test_settings.model_copy(deep=True)
        # .com, not .example: "example" is itself an RFC-reserved TLD.
        settings.mail.reserved_domain = "real-company.example.com"
        application = create_app(settings)

        transport = ASGITransport(app=application)
        async with AsyncClient(
            transport=transport, base_url="http://nexus.test", headers={"X-API-Key": TEST_API_KEY}
        ) as client:
            response = await client.post("/api/v1/inboxes", json={})

        assert response.status_code == 403
        assert response.json()["code"] == "email_domain_not_allowed"

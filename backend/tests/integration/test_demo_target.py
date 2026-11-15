"""Tests for the bundled demonstration application.

The demo target is the default thing NEXUS tests against, so it must itself behave correctly: a
buggy target produces meaningless registration results. These tests treat it as a black box and
drive it over HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from demo_target.app import CallbackMailer, create_demo_app, hash_password, verify_password_hash


def _mailbox() -> tuple[list[dict[str, str]], CallbackMailer]:
    """Build a mailer that records messages instead of sending them."""
    captured: list[dict[str, str]] = []

    def _deliver(to: str, subject: str, text: str) -> None:
        captured.append({"to": to, "subject": subject, "text": text})

    return captured, CallbackMailer(_deliver)


@pytest.fixture
def demo():
    """A demo application plus its captured mail."""
    captured, mailer = _mailbox()
    app = create_demo_app(mailer=mailer)
    return app, captured


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo.test")


def _code_from(text: str) -> str:
    import re

    match = re.search(r"\b(\d{6})\b", text)
    assert match is not None, f"no code in {text!r}"
    return match.group(1)


class TestRegistration:
    """The registration flow itself."""

    async def test_register_emails_a_code(self, demo) -> None:
        app, captured = demo
        async with _client(app) as client:
            response = await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
        assert response.status_code == 202
        assert response.json()["status"] == "pending_verification"
        assert len(captured) == 1
        assert captured[0]["to"] == "qa-1@example.test"
        assert _code_from(captured[0]["text"])

    async def test_duplicate_email_is_rejected(self, demo) -> None:
        app, _captured = demo
        payload = {"email": "qa-1@example.test", "password": "password123"}
        async with _client(app) as client:
            await client.post("/api/register", json=payload)
            second = await client.post("/api/register", json=payload)
        assert second.status_code == 409

    async def test_short_password_is_rejected(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            response = await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "short"}
            )
        assert response.status_code == 422

    async def test_invalid_email_is_rejected(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            response = await client.post(
                "/api/register", json={"email": "not-an-email", "password": "password123"}
            )
        assert response.status_code == 422

    async def test_email_is_lowercased(self, demo) -> None:
        app, captured = demo
        async with _client(app) as client:
            await client.post(
                "/api/register", json={"email": "QA-1@Example.TEST", "password": "password123"}
            )
        assert captured[0]["to"] == "qa-1@example.test"


class TestVerification:
    """Completing registration with the emailed code."""

    async def test_correct_code_activates_the_account(self, demo) -> None:
        app, captured = demo
        async with _client(app) as client:
            await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
            code = _code_from(captured[0]["text"])
            response = await client.post(
                "/api/verify", json={"email": "qa-1@example.test", "code": code}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "active"

            status = await client.get("/api/status/qa-1@example.test")
        assert status.json()["status"] == "active"

    async def test_wrong_code_is_rejected(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
            response = await client.post(
                "/api/verify", json={"email": "qa-1@example.test", "code": "000000"}
            )
        assert response.status_code == 400

    async def test_unknown_account_is_404(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            response = await client.post(
                "/api/verify", json={"email": "nobody@example.test", "code": "123456"}
            )
        assert response.status_code == 404

    async def test_expired_code_is_410(self, demo) -> None:
        app, captured = demo
        async with _client(app) as client:
            await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
            # Move the deadline into the past.
            user = app.state.store.all_users()[0]
            user.code_expires_at = datetime.now(UTC) - timedelta(minutes=1)
            code = _code_from(captured[0]["text"])
            response = await client.post(
                "/api/verify", json={"email": "qa-1@example.test", "code": code}
            )
        assert response.status_code == 410

    async def test_status_of_unknown_account_is_404(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            assert (await client.get("/api/status/nobody@example.test")).status_code == 404

    async def test_status_before_verification_is_pending(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
            body = (await client.get("/api/status/qa-1@example.test")).json()
        assert body["status"] == "pending_verification"


class TestSimulatedControls:
    """The simulation switch makes the 'stop on a control' path testable."""

    async def test_rate_limit(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            await client.post("/api/_simulate", json={"mode": "rate_limit"})
            response = await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
        assert response.status_code == 429

    async def test_captcha(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            await client.post("/api/_simulate", json={"mode": "captcha"})
            response = await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
        assert response.status_code == 400
        assert "captcha" in response.text.lower()

    async def test_access_denied(self, demo) -> None:
        app, _captured = demo
        async with _client(app) as client:
            await client.post("/api/_simulate", json={"mode": "access_denied"})
            response = await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
        assert response.status_code == 403

    async def test_reset_to_none(self, demo) -> None:
        app, captured = demo
        async with _client(app) as client:
            await client.post("/api/_simulate", json={"mode": "rate_limit"})
            await client.post("/api/_simulate", json={"mode": "none"})
            response = await client.post(
                "/api/register", json={"email": "qa-1@example.test", "password": "password123"}
            )
        assert response.status_code == 202
        assert captured


class TestPasswordHashing:
    """The demo keeps its own hashing, independent of NEXUS's."""

    def test_hash_verifies(self) -> None:
        hashed = hash_password("password123")
        assert verify_password_hash("password123", hashed)
        assert not verify_password_hash("password124", hashed)

    def test_hash_is_salted(self) -> None:
        assert hash_password("password123") != hash_password("password123")

    def test_hash_does_not_contain_the_password(self) -> None:
        assert "password123" not in hash_password("password123")

    def test_garbage_hash_returns_false(self) -> None:
        assert verify_password_hash("x", "garbage") is False

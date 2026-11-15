"""Mail backend tests.

Two backends, one contract. The in-memory backend is exercised directly; the Mailpit backend is
tested through an injected transport, so its JSON parsing is verified without a running Mailpit
and without any network access.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.core.exceptions import ExternalServiceError
from app.modules.email_manager.backends import (
    InMemoryMailBackend,
    MailpitBackend,
    get_backend,
    reset_backend,
)


class TestInMemoryBackend:
    """The backend used by tests and offline work."""

    async def test_delivered_message_is_listed_for_its_recipient(self) -> None:
        backend = InMemoryMailBackend()
        backend.deliver(recipient="qa-1@example.test", body_text="hello")

        assert len(await backend.list_ids("qa-1@example.test")) == 1

    async def test_messages_are_isolated_by_recipient(self) -> None:
        backend = InMemoryMailBackend()
        backend.deliver(recipient="a@example.test")
        backend.deliver(recipient="b@example.test")

        assert len(await backend.list_ids("a@example.test")) == 1

    async def test_recipient_matching_is_case_insensitive(self) -> None:
        backend = InMemoryMailBackend()
        backend.deliver(recipient="QA-1@Example.TEST")
        assert len(await backend.list_ids("qa-1@example.test")) == 1

    async def test_fetch_returns_the_message(self) -> None:
        backend = InMemoryMailBackend()
        message_id = backend.deliver(
            recipient="a@example.test", subject="Hi", body_text="Your code is 481920"
        )

        message = await backend.fetch(message_id)
        assert message is not None
        assert message.subject == "Hi"
        assert "481920" in (message.body_text or "")

    async def test_fetch_unknown_id_returns_none(self) -> None:
        assert await InMemoryMailBackend().fetch("nope") is None

    async def test_newest_first(self) -> None:
        backend = InMemoryMailBackend()
        older = datetime(2026, 1, 1, tzinfo=UTC)
        newer = datetime(2026, 6, 1, tzinfo=UTC)
        first = backend.deliver(recipient="a@example.test", received_at=older)
        second = backend.deliver(recipient="a@example.test", received_at=newer)

        assert await backend.list_ids("a@example.test") == [second, first]

    async def test_limit_is_respected(self) -> None:
        backend = InMemoryMailBackend()
        for _ in range(5):
            backend.deliver(recipient="a@example.test")
        assert len(await backend.list_ids("a@example.test", limit=2)) == 2

    async def test_health_is_always_true(self) -> None:
        assert await InMemoryMailBackend().health() is True

    async def test_clear_removes_everything(self) -> None:
        backend = InMemoryMailBackend()
        backend.deliver(recipient="a@example.test")
        backend.clear()
        assert backend.message_count == 0


class TestMailpitParsing:
    """The Mailpit adapter, driven by a fake transport."""

    def _backend(self, test_settings, handler) -> MailpitBackend:  # type: ignore[no-untyped-def]
        return MailpitBackend(test_settings, transport=httpx.MockTransport(handler))

    async def test_list_ids_asks_mailpit_for_the_recipient(self, test_settings) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["query"] = request.url.params.get("query")
            return httpx.Response(200, json={"messages": [{"ID": "m1"}, {"ID": "m2"}]})

        backend = self._backend(test_settings, handler)
        ids = await backend.list_ids("qa-1@example.test")

        assert ids == ["m1", "m2"]
        assert seen["path"] == "/api/v1/search"
        # The address must be sent as a parameter value, not concatenated into the URL.
        assert seen["query"] == "to:qa-1@example.test"

    async def test_list_ids_ignores_malformed_entries(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"messages": [{"ID": "ok"}, {"nope": 1}, "junk"]})

        backend = self._backend(test_settings, handler)
        assert await backend.list_ids("a@example.test") == ["ok"]

    async def test_list_ids_rejects_an_unexpected_shape(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"messages": "not-a-list"})

        backend = self._backend(test_settings, handler)
        with pytest.raises(ExternalServiceError):
            await backend.list_ids("a@example.test")

    async def test_list_ids_wraps_an_http_error(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        backend = self._backend(test_settings, handler)
        with pytest.raises(ExternalServiceError, match="rejected"):
            await backend.list_ids("a@example.test")

    async def test_list_ids_wraps_a_transport_error(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        backend = self._backend(test_settings, handler)
        with pytest.raises(ExternalServiceError, match="could not be reached"):
            await backend.list_ids("a@example.test")

    async def test_fetch_parses_a_full_message(self, test_settings) -> None:
        payload = {
            "ID": "abc123",
            "From": {"Address": "no-reply@demo.test", "Name": "Demo"},
            "To": [{"Address": "qa-1@example.test", "Name": ""}],
            "Subject": "Your verification code",
            "Text": "Your code is 481920",
            "HTML": "<p>Your code is <b>481920</b></p>",
            "Created": "2026-11-15T18:20:11.123Z",
            "Size": 512,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        backend = self._backend(test_settings, handler)
        message = await backend.fetch("abc123")

        assert message is not None
        assert message.backend_id == "abc123"
        assert message.sender == "no-reply@demo.test"
        assert message.recipient == "qa-1@example.test"
        assert message.subject == "Your verification code"
        assert message.body_text == "Your code is 481920"
        assert message.body_html is not None
        assert message.received_at.year == 2026
        assert message.size_bytes == 512

    async def test_fetch_returns_none_for_404(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        backend = self._backend(test_settings, handler)
        assert await backend.fetch("gone") is None

    async def test_fetch_tolerates_odd_field_shapes(self, test_settings) -> None:
        # Mailpit represents From/To differently across versions; a missing field must degrade
        # that field, not lose the whole message.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ID": "x", "From": "plain@example.test", "To": "qa@example.test"}
            )

        backend = self._backend(test_settings, handler)
        message = await backend.fetch("x")
        assert message is not None
        assert message.sender == "plain@example.test"
        assert message.recipient == "qa@example.test"
        assert message.subject is None

    async def test_oversized_message_is_skipped(self, test_settings) -> None:
        settings = test_settings.model_copy(deep=True)
        settings.mail.max_message_bytes = 1024
        big_body = "x" * 5000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"ID": "big", "Text": big_body, "From": "a@b.test", "To": ["q@b.test"]}
            )

        backend = MailpitBackend(settings, transport=httpx.MockTransport(handler))
        assert await backend.fetch("big") is None

    async def test_unparseable_json_returns_none(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{not json")

        backend = self._backend(test_settings, handler)
        assert await backend.fetch("x") is None

    async def test_health_true_when_reachable(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Version": "1.0"})

        backend = self._backend(test_settings, handler)
        assert await backend.health() is True

    async def test_health_false_when_unreachable(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        backend = self._backend(test_settings, handler)
        assert await backend.health() is False

    async def test_invalid_timestamp_falls_back_to_now(self, test_settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ID": "x", "Created": "not-a-date", "From": "a@b.test", "To": ["q@b.test"]},
            )

        backend = self._backend(test_settings, handler)
        message = await backend.fetch("x")
        assert message is not None
        # Losing a message because its date was odd would be worse than a slightly wrong date.
        assert message.received_at is not None


class TestBackendFactory:
    """Configuration selects the backend."""

    def test_memory_backend_is_selected_by_default(self, test_settings) -> None:
        reset_backend()
        assert isinstance(get_backend(test_settings), InMemoryMailBackend)
        reset_backend()

    def test_mailpit_backend_is_selected_when_configured(self, test_settings) -> None:
        settings = test_settings.model_copy(deep=True)
        settings.mail.backend = "mailpit"
        reset_backend()
        assert isinstance(get_backend(settings), MailpitBackend)
        reset_backend()

    def test_factory_returns_the_same_instance(self, test_settings) -> None:
        reset_backend()
        first = get_backend(test_settings)
        assert get_backend(test_settings) is first
        reset_backend()

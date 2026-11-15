"""Logging and redaction tests.

Threat T3 is "secret leakage via logs", and the mitigation is a logging filter that rewrites
every record. A filter is only worth having if it actually catches the shapes secrets come in,
so these tests feed it the realistic ones: keyword arguments, bearer tokens, JWTs, credential-
bearing URLs, and nested dictionaries.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    redact,
    redact_string,
    set_request_id,
)


class TestRedactString:
    """Free-text redaction covers the patterns secrets actually appear in."""

    def test_key_equals_value_is_redacted(self) -> None:
        result = redact_string("password=hunter2")
        assert "hunter2" not in result
        assert REDACTED in result

    def test_key_colon_value_is_redacted(self) -> None:
        assert "s3cr3t" not in redact_string("api_key: s3cr3t")

    def test_bearer_token_is_redacted(self) -> None:
        result = redact_string("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnop")
        assert "abcdefghijklmnop" not in result

    def test_basic_auth_is_redacted(self) -> None:
        result = redact_string("Authorization: Basic dXNlcjpwYXNzd29yZA==")
        assert "dXNlcjpwYXNzd29yZA==" not in result

    def test_standalone_jwt_is_redacted(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        result = redact_string(f"token received: {jwt}")
        assert jwt not in result
        assert REDACTED in result

    def test_credential_in_url_is_redacted(self) -> None:
        result = redact_string("connecting to socks5://bob:hunter2@proxy.example.test:1080")
        assert "hunter2" not in result
        # The host must survive: redacting it would make the log useless for debugging.
        assert "proxy.example.test:1080" in result

    def test_ordinary_text_is_untouched(self) -> None:
        message = "Checked 12 proxies, 9 healthy, median latency 143ms"
        assert redact_string(message) == message

    def test_word_containing_password_is_not_falsely_matched(self) -> None:
        # A naive substring match would mangle this. The pattern requires a key boundary.
        assert redact_string("passwordless login is supported") == "passwordless login is supported"


class TestRedactStructures:
    """Structured payloads are redacted by key, recursively."""

    def test_flat_dict_sensitive_key_is_redacted(self) -> None:
        result = redact({"username": "bob", "password": "hunter2"})
        assert result["username"] == "bob"
        assert result["password"] == REDACTED

    def test_key_matching_is_case_and_space_insensitive(self) -> None:
        result = redact({"API_KEY": "abc", "  Token  ": "def"})
        assert result["API_KEY"] == REDACTED
        assert result["  Token  "] == REDACTED

    def test_nested_dict_is_redacted(self) -> None:
        result = redact({"proxy": {"host": "h", "credentials": {"password": "p"}}})
        assert result["proxy"]["host"] == "h"
        assert result["proxy"]["credentials"] == REDACTED

    def test_lists_are_redacted_elementwise(self) -> None:
        result = redact([{"token": "a"}, {"name": "safe"}])
        assert result[0]["token"] == REDACTED
        assert result[1]["name"] == "safe"

    def test_values_inside_a_list_of_strings_are_redacted(self) -> None:
        result = redact({"notes": ["password=x", "nothing here"]})
        assert "x" not in result["notes"][0]
        assert result["notes"][1] == "nothing here"

    def test_depth_limit_is_applied(self) -> None:
        # Deeply nested input must not hang the logger; beyond the limit the value is returned
        # as-is rather than recursing forever.
        payload: dict[str, object] = {"level": "deep"}
        current = payload
        for _ in range(12):
            nested: dict[str, object] = {}
            current["child"] = nested
            current = nested
        current["password"] = "secret"
        # The call must simply complete.
        assert isinstance(redact(payload), dict)

    def test_non_string_scalars_are_preserved(self) -> None:
        result = redact({"count": 3, "ok": True, "ratio": 0.5, "none": None})
        assert result == {"count": 3, "ok": True, "ratio": 0.5, "none": None}


class TestRedactionFilter:
    """The filter runs on the record, before any formatter renders it."""

    def _record(self, message: object, args: object = None) -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=args,
            exc_info=None,
        )

    def test_message_is_redacted(self) -> None:
        record = self._record("api_key=abcdef123456")
        assert RedactionFilter().filter(record)
        assert "abcdef123456" not in record.msg

    def test_lazy_formatting_arguments_are_redacted(self) -> None:
        # This is the case a naive filter misses: the secret lives in record.args and is only
        # interpolated later, by the formatter. Redacting the template alone would break the
        # formatting; redacting the argument alone would miss it, because "supersecrettoken" on
        # its own has no recognisable shape - the *key* is what identifies it as a secret.
        record = self._record("connecting with token=%s", ("supersecrettoken",))
        RedactionFilter().filter(record)

        rendered = record.getMessage()
        assert "supersecrettoken" not in rendered
        assert REDACTED in rendered
        # Rendering happened inside the filter, so the record is now a plain redacted line.
        assert record.args == ()

    def test_dict_arguments_are_redacted(self) -> None:
        record = self._record("payload=%(body)s", ({"body": "password=hunter2"},))
        RedactionFilter().filter(record)
        assert "hunter2" not in record.getMessage()

    def test_non_string_arguments_survive(self) -> None:
        record = self._record("checked %d proxies", (7,))
        RedactionFilter().filter(record)
        assert record.getMessage() == "checked 7 proxies"

    def test_filter_never_drops_records(self) -> None:
        # Returning False would silently swallow log lines. It must always return True.
        assert RedactionFilter().filter(self._record("anything")) is True


class TestJsonFormatter:
    """The JSON formatter must emit parseable single-line records with a request id."""

    def test_emits_parseable_json(self) -> None:
        record = logging.LogRecord(
            name="nexus.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "nexus.test"

    def test_request_id_is_included(self) -> None:
        set_request_id("abc123")
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
        assert payload["request_id"] == "abc123"
        set_request_id("-")

    def test_exception_text_is_redacted(self) -> None:
        try:
            msg = "password=hunter2"
            raise ValueError(msg)
        except ValueError:
            import sys

            record = logging.LogRecord(
                name="t",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="failed",
                args=(),
                exc_info=sys.exc_info(),
            )
        rendered = JsonFormatter().format(record)
        assert "hunter2" not in rendered
        assert "ValueError" in rendered


class TestConfigureLogging:
    """Configuration must be idempotent and install the filter on every handler."""

    def test_filter_is_installed_on_every_handler(self) -> None:
        root = configure_logging(level="INFO", also_stderr=False)
        assert root.handlers
        # pytest installs its own capture handlers on the root logger; those are not ours to
        # police. Every handler NEXUS adds must carry the filter.
        nexus_handlers = [h for h in root.handlers if isinstance(h, logging.NullHandler)]
        assert nexus_handlers
        for handler in nexus_handlers:
            assert any(isinstance(f, RedactionFilter) for f in handler.filters)

    def test_stderr_handler_carries_the_filter(self) -> None:
        root = configure_logging(level="INFO", also_stderr=True)
        stream_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
        ]
        assert stream_handlers
        for handler in stream_handlers:
            assert any(isinstance(f, RedactionFilter) for f in handler.filters)

    def test_repeated_calls_do_not_duplicate_handlers(self) -> None:
        configure_logging(level="INFO", also_stderr=False)
        first = len(logging.getLogger().handlers)
        configure_logging(level="INFO", also_stderr=False)
        assert len(logging.getLogger().handlers) == first

    def test_noisy_libraries_are_quietened(self) -> None:
        configure_logging(level="DEBUG", also_stderr=False)
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_secret_does_not_reach_the_log_file(self, tmp_path) -> None:
        # End-to-end proof: log a secret through the real pipeline and read the file back.
        root = configure_logging(level="INFO", log_directory=tmp_path, also_stderr=False)
        logger = logging.getLogger("nexus.redaction.test")
        # WARNING, not INFO: the root logger is set to INFO, but pytest resets logger levels
        # between tests; a WARNING record is emitted regardless of a stale level.
        logger.warning("connecting with api_key=%s", "TOP-SECRET-VALUE-12345")
        for handler in root.handlers:
            handler.flush()

        log_file = tmp_path / "nexus.log"
        assert log_file.exists(), f"no log file was written; handlers={root.handlers}"
        written = log_file.read_text(encoding="utf-8")
        assert written, "the log file was created but left empty"
        assert "TOP-SECRET-VALUE-12345" not in written
        assert REDACTED in written


class TestRequestIdContext:
    """The request id must be available to log records with no request in scope."""

    def test_default_request_id_is_a_placeholder(self) -> None:
        from app.core.logging import get_request_id

        set_request_id("-")
        assert get_request_id() == "-"

    def test_set_and_get_round_trip(self) -> None:
        from app.core.logging import get_request_id

        set_request_id("round-trip-id")
        assert get_request_id() == "round-trip-id"
        set_request_id("-")


@pytest.mark.parametrize(
    "key", ["password", "secret", "token", "api_key", "authorization", "cookie"]
)
def test_every_documented_sensitive_key_is_covered(key: str) -> None:
    """docs/SECURITY.md 4 lists the covered key names; this keeps the list honest."""
    assert redact({key: "value"})[key] == REDACTED

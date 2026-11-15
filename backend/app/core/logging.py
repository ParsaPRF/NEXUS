"""Structured application logging with mandatory secret redaction.

Why a custom module instead of ``logging.basicConfig``?
-------------------------------------------------------
Threat T3 (docs/SECURITY.md) is "secret leakage via logs". The only reliable way to stop that
is to make redaction impossible to bypass: it happens inside a ``logging.Filter`` attached to
the root handler, so *every* record from *every* logger passes through it. A developer cannot
forget to call it.

Two output formats are supported:

* ``console`` - human readable, used while developing.
* ``json``   - one JSON object per line, used in containers where a log collector parses it.

A ``request_id`` context variable is attached to every record so a single user request can be
traced across modules and correlated with ``audit_logs.request_id``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Request correlation
# --------------------------------------------------------------------------------------

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(value: str) -> None:
    """Bind a request id to the current context (called by middleware)."""
    _request_id.set(value)


def get_request_id() -> str:
    """Return the request id bound to the current context."""
    return _request_id.get()


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------

REDACTED = "***REDACTED***"

#: Field names whose values must never reach a log file.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "x-api-key",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "cookie",
        "set-cookie",
        "session",
        "private_key",
    }
)

#: Key names ordered longest-first. This ordering is essential in a regex alternation: with
#: alphabetical order, ``api`` would match before ``api_key``, the remaining ``_key`` would fail
#: the lookahead, and the secret would sail through unredacted.
_SENSITIVE_KEY_PATTERN = "|".join(
    sorted((re.escape(key) for key in SENSITIVE_KEYS), key=len, reverse=True)
)

#: Patterns for secrets that appear *inside* free text rather than as a field name.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token>
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
    # key=value / key: value where the key looks sensitive.
    # The leading boundary is fixed-width so the match does not consume surrounding spaces or
    # quotes; otherwise re-running redaction over already-redacted text would accumulate
    # whitespace (the filter can legitimately see a record twice).
    re.compile(
        r"(?i)(?<![\w])(" + _SENSITIVE_KEY_PATTERN + r")(?![\w])\s*(?:=|:)\s*"
        r"(\"[^\"]*\"|'[^']*'|\S+)"
    ),
    # JWT shape: three base64url segments separated by dots
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    # scheme://user:password@host
    re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^/\s:@]+):([^@/\s]+)@"),
)


def redact_string(value: str) -> str:
    """Return ``value`` with secret-shaped substrings replaced by ``***REDACTED***``.

    Used both by the log filter and before persisting payloads into
    ``background_jobs.payload_json`` / ``audit_logs.detail_json``.

    Idempotent by construction: the replacement is the literal ``***REDACTED***``, which does
    not itself match any pattern, so applying this function twice produces the same result.
    """
    result = value
    result = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)} {REDACTED}", result)
    result = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}={REDACTED}", result)
    result = _SECRET_PATTERNS[2].sub(REDACTED, result)
    return _SECRET_PATTERNS[3].sub(lambda m: f"{m.group(1)}:{REDACTED}@", result)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys in mappings and sequences.

    Depth-limited so a cyclic or pathologically nested payload cannot hang a log call.
    """
    if _depth > 6:
        return value
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in SENSITIVE_KEYS:
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact(item, _depth=_depth + 1)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_string(value)
    return value


class RedactionFilter(logging.Filter):
    """Rewrite every log record so it cannot carry a secret.

    The subtle part is *how* the message is handled. A log call such as::

        logger.info("connecting with token=%s", token)

    stores the template ``"connecting with token=%s"`` in ``record.msg`` and the secret in
    ``record.args``; the two are only combined later, by the formatter. If this filter were to
    redact ``record.msg`` and leave ``args`` alone, the ``%s`` placeholder would be replaced
    inside the template and ``record.getMessage()`` would then raise
    ``TypeError: not all arguments converted``.

    So the rule is:

    * when ``args`` are present, the *template* is left intact (the placeholder must survive)
      and the *arguments* are redacted - which is where the secret actually is;
    * when there are no args, the message itself is redacted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact a record in place.

        The hard case is lazy formatting::

            logger.warning("api_key=%s", secret)

        Here the template is ``"api_key=%s"`` and the secret is only in ``args``, as a bare
        value with no recognisable shape. Redacting the template breaks the formatting; redacting
        the argument alone misses the secret entirely, because ``"hunter2"`` on its own matches no
        pattern - the *key* is what identifies it.

        So the record is **rendered first, then redacted**. That loses the deferred-formatting
        optimisation, which is irrelevant here (the cost is one string operation per emitted log
        line), and it is the only way to apply key-aware patterns to the completed message.

        If rendering fails - a malformed call such as a stray placeholder - we fall back to
        redacting the template and the arguments independently. A logging call must never raise.
        """
        if record.args:
            try:
                rendered = record.getMessage()
            except (TypeError, ValueError):
                rendered = None

            if rendered is not None:
                record.msg = redact_string(rendered)
                record.args = ()
            else:  # pragma: no cover - defensive, malformed logging call
                if isinstance(record.msg, str):
                    record.msg = redact_string(record.msg)
                if isinstance(record.args, dict):
                    record.args = redact(record.args)
                else:
                    record.args = tuple(
                        redact_string(arg) if isinstance(arg, str) else redact(arg)
                        for arg in record.args
                    )
        elif isinstance(record.msg, str):
            # No arguments: any secret is in the message text itself.
            record.msg = redact_string(record.msg)
        elif isinstance(record.msg, dict):
            record.msg = redact(record.msg)

        # Exception tracebacks can embed interpolated values too.
        if record.exc_text:
            record.exc_text = redact_string(record.exc_text)
        return True


# --------------------------------------------------------------------------------------
# Formatters
# --------------------------------------------------------------------------------------

_EXTRA_ATTRS = ("request_id", "module_name", "job_id")


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line format."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(name)s] [%(request_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


class JsonFormatter(logging.Formatter):
    """One JSON object per line, ready for a log collector."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # Falls back to the context variable so the formatter is correct even when a record
            # was never passed through the handler filters (e.g. in a unit test).
            "request_id": getattr(record, "request_id", None) or get_request_id(),
            "module": getattr(record, "module_name", record.module),
        }
        for attr in _EXTRA_ATTRS:
            if (value := getattr(record, attr, None)) is not None and attr not in payload:
                payload[attr] = value
        if record.exc_info:
            payload["exception"] = redact_string(self.formatException(record.exc_info))
        return json.dumps(redact(payload), ensure_ascii=False, default=str)


# --------------------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------------------


class _RequestIdFilter(logging.Filter):
    """Ensure ``%(request_id)s`` always exists, even outside a request context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger.

    A one-line wrapper, but a useful one: it gives every module a single import for logging and
    makes it obvious in review that the redaction filter (installed on the root handlers) is
    what protects these calls.
    """
    return logging.getLogger(name)


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "console",
    log_directory: Path | None = None,
    also_stderr: bool = True,
) -> logging.Logger:
    """Configure the root logger. Safe to call more than once.

    Args:
        level: A standard level name, e.g. ``"INFO"``.
        log_format: ``"console"`` or ``"json"``.
        log_directory: When given, logs are also written to ``<dir>/nexus.log``.
        also_stderr: Keep writing to stderr (disable in tests to keep output clean).

    Returns:
        The configured root logger.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Idempotent: drop handlers we added previously so repeated calls do not duplicate lines.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = JsonFormatter() if log_format == "json" else ConsoleFormatter()
    handlers: list[logging.Handler] = []

    if also_stderr:
        stream = logging.StreamHandler(sys.stderr)
        handlers.append(stream)

    if log_directory is not None:
        log_directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_directory / "nexus.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    for handler in handlers:
        handler.setFormatter(formatter)
        # Order matters: redact first, then guarantee request_id exists.
        handler.addFilter(RedactionFilter())
        handler.addFilter(_RequestIdFilter())
        root.addHandler(handler)

    if not handlers:
        # Nothing to write to, but keep the filter chain available for caplog and for tests
        # that assert the redaction filter is installed.
        null_handler = logging.NullHandler()
        null_handler.addFilter(RedactionFilter())
        null_handler.addFilter(_RequestIdFilter())
        root.addHandler(null_handler)

    # These libraries log a lot at INFO and are rarely useful at that level.
    for noisy in ("httpx", "httpcore", "uvicorn.access", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root

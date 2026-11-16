"""Small formatting helpers shared by the views.

Kept separate from the widgets so they can be unit-tested without Qt at all.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API, tolerating a trailing ``Z`` and nulls."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_timestamp(value: object, *, with_date: bool = False, local: bool = True) -> str:
    """Render a timestamp for the UI.

    The API always sends UTC; showing it in the operator's local zone is what makes "when did this
    run" answerable at a glance.
    """
    parsed = parse_timestamp(value)
    if parsed is None:
        return ""
    if local:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def format_duration_ms(value: object) -> str:
    """Render a millisecond duration compactly."""
    if not isinstance(value, (int, float)):
        return ""
    milliseconds = int(value)
    if milliseconds < 1000:  # noqa: PLR2004
        return f"{milliseconds} ms"
    seconds = milliseconds / 1000
    if seconds < 60:  # noqa: PLR2004
        return f"{seconds:.1f} s"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds}s"


def shorten(value: object, limit: int = 60) -> str:
    """Truncate long text for a table cell."""
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def yes_no(value: object) -> str:
    """Render a boolean as yes/no, or an empty string for null."""
    if value is None:
        return ""
    return "yes" if value else "no"


__all__ = [
    "format_duration_ms",
    "format_timestamp",
    "parse_timestamp",
    "shorten",
    "yes_no",
]

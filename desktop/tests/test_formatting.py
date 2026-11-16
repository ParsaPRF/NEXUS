"""Formatting helper tests (no Qt)."""

from __future__ import annotations

from app.formatting import (
    format_duration_ms,
    format_timestamp,
    parse_timestamp,
    shorten,
    yes_no,
)


class TestTimestamps:
    """Timestamps arrive as UTC and are shown in local time."""

    def test_parses_z_suffix(self) -> None:
        parsed = parse_timestamp("2026-11-16T04:00:00Z")
        assert parsed is not None
        assert parsed.year == 2026

    def test_missing_values_return_none(self) -> None:
        assert parse_timestamp(None) is None
        assert parse_timestamp("") is None

    def test_garbage_returns_none(self) -> None:
        # A malformed timestamp must not take a whole table down.
        assert parse_timestamp("not-a-date") is None

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        parsed = parse_timestamp("2026-11-16T04:00:00")
        assert parsed is not None
        assert parsed.tzinfo is not None

    def test_formats_time_only_by_default(self) -> None:
        rendered = format_timestamp("2026-11-16T04:00:00Z")
        assert len(rendered) == 8  # HH:MM:SS
        assert rendered.count(":") == 2  # noqa: PLR2004

    def test_formats_with_date_when_asked(self) -> None:
        rendered = format_timestamp("2026-11-16T04:00:00Z", with_date=True)
        assert rendered.startswith("2026-11-16")

    def test_empty_for_missing(self) -> None:
        assert format_timestamp(None) == ""


class TestDurations:
    """Durations render compactly."""

    def test_milliseconds(self) -> None:
        assert format_duration_ms(412) == "412 ms"

    def test_seconds(self) -> None:
        assert format_duration_ms(2500) == "2.5 s"

    def test_minutes(self) -> None:
        assert format_duration_ms(125_000) == "2m 5s"

    def test_missing(self) -> None:
        assert format_duration_ms(None) == ""


class TestText:
    """Small text helpers."""

    def test_shorten_keeps_short_text(self) -> None:
        assert shorten("abc", 10) == "abc"

    def test_shorten_truncates_with_ellipsis(self) -> None:
        result = shorten("a" * 50, 10)
        assert len(result) == 10  # noqa: PLR2004
        assert result.endswith("…")

    def test_yes_no(self) -> None:
        assert yes_no(True) == "yes"
        assert yes_no(False) == "no"
        assert yes_no(None) == ""

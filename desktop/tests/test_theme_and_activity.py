"""Activity log and theme tests (no widgets beyond the Qt application object)."""

from __future__ import annotations

from app.activity import MAX_ENTRIES, ActivityLog
from app.theme import COLORS, STATUS_COLORS, build_palette, build_stylesheet, color_for_status


class TestActivityLog:
    """The client-side log is bounded and observable."""

    def test_records_entries(self, qapp) -> None:  # type: ignore[no-untyped-def]
        log = ActivityLog()
        log.info("started")
        log.error("failed")
        assert [entry.level for entry in log.entries] == ["info", "error"]

    def test_emits_a_signal_per_entry(self, qapp) -> None:  # type: ignore[no-untyped-def]
        log = ActivityLog()
        received: list[str] = []
        log.appended.connect(lambda entry: received.append(entry.message))
        log.info("hello")
        assert received == ["hello"]

    def test_is_bounded(self, qapp) -> None:  # type: ignore[no-untyped-def]
        # An unbounded in-memory log is a slow memory leak in a long-running desktop session.
        log = ActivityLog(max_entries=5)
        for index in range(20):
            log.info(f"entry {index}")
        assert len(log.entries) == 5  # noqa: PLR2004
        assert log.entries[-1].message == "entry 19"

    def test_clear(self, qapp) -> None:  # type: ignore[no-untyped-def]
        log = ActivityLog()
        log.info("x")
        log.clear()
        assert log.entries == []

    def test_default_cap_is_reasonable(self) -> None:
        assert 100 <= MAX_ENTRIES <= 10_000

    def test_time_text_is_seconds_resolution(self, qapp) -> None:  # type: ignore[no-untyped-def]
        entry = ActivityLog().info("x")
        assert len(entry.time_text) == 8  # noqa: PLR2004


class TestTheme:
    """The dark theme is defined once and referenced by role."""

    def test_stylesheet_contains_the_palette(self) -> None:
        sheet = build_stylesheet()
        assert COLORS["bg"] in sheet
        assert COLORS["accent"] in sheet

    def test_stylesheet_styles_tables_and_cards(self) -> None:
        sheet = build_stylesheet()
        assert "QTableView" in sheet
        assert "#Card" in sheet
        assert "QProgressBar" in sheet

    def test_palette_is_dark(self) -> None:
        palette = build_palette()
        window = palette.color(palette.ColorRole.Window)
        text = palette.color(palette.ColorRole.WindowText)
        # A dark theme by definition: background darker than the text.
        assert window.lightness() < text.lightness()

    def test_status_colours_cover_every_backend_status(self) -> None:
        for status in [
            "healthy",
            "unknown",
            "unhealthy",
            "expired",
            "active",
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
            "stopped_by_target",
            "success",
            "verification_failed",
            "email_timeout",
            "rate_limited",
            "captcha_encountered",
            "access_denied",
        ]:
            assert status in STATUS_COLORS, status

    def test_unknown_status_falls_back_to_neutral(self) -> None:
        assert color_for_status("something-new").name() == COLORS["neutral"]

    def test_status_lookup_is_case_insensitive(self) -> None:
        assert color_for_status("HEALTHY").name() == color_for_status("healthy").name()

"""Widget and threading tests.

Two things are verified here that a pure-logic test cannot reach:

* the table model maps JSON keys to columns, formats them and colours status cells;
* :class:`~app.tasks.TaskRunner` really does the work **off the GUI thread** and reports back
  through signals - which is the specific requirement "the interface must remain responsive while
  background jobs execute".
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt

from app.api_client import ApiError, NexusApiError
from app.tasks import TaskRunner
from app.widgets import Column, DictTableModel, StatCard, button, page_header, toolbar


class TestDictTableModel:
    """The model that every table in the application uses."""

    def _model(self) -> DictTableModel:
        return DictTableModel(
            [
                Column("id", "Id", 40),
                Column("host", "Host"),
                Column("status", "Status", colorize=True),
                Column("latency_ms", "Latency", formatter=lambda value: f"{value} ms"),
                Column("is_enabled", "Enabled"),
            ]
        )

    def test_reports_dimensions(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        model.set_rows([{"id": 1, "host": "a"}, {"id": 2, "host": "b"}])
        assert model.rowCount() == 2  # noqa: PLR2004
        assert model.columnCount() == 5  # noqa: PLR2004

    def test_headers_come_from_the_columns(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        assert model.headerData(1, Qt.Orientation.Horizontal) == "Host"

    def test_display_role_renders_values(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        model.set_rows([{"id": 7, "host": "proxy.example.test"}])
        index = model.index(0, 1)
        assert model.data(index, Qt.ItemDataRole.DisplayRole) == "proxy.example.test"

    def test_formatter_is_applied(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        model.set_rows([{"id": 1, "latency_ms": 42}])
        assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == "42 ms"

    def test_missing_values_render_empty(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        model.set_rows([{"id": 1}])
        assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == ""

    def test_booleans_render_as_words(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        model.set_rows([{"id": 1, "is_enabled": False}])
        assert model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole) == "no"

    def test_status_column_is_coloured(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        model.set_rows([{"id": 1, "status": "healthy"}])
        healthy = model.data(model.index(0, 2), Qt.ItemDataRole.ForegroundRole)
        model.set_rows([{"id": 1, "status": "expired"}])
        expired = model.data(model.index(0, 2), Qt.ItemDataRole.ForegroundRole)
        assert healthy is not None and expired is not None
        assert healthy.name() != expired.name()

    def test_rows_are_copied(self, qapp) -> None:  # type: ignore[no-untyped-def]
        # Mutating the caller's list must not silently change what the table shows.
        model = self._model()
        source = [{"id": 1, "host": "a"}]
        model.set_rows(source)
        source[0]["host"] = "changed"
        assert model.value(0, "host") == "a"

    def test_row_at_returns_the_row(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        model.set_rows([{"id": 5, "host": "h"}])
        row = model.row_at(model.index(0, 0))
        assert row is not None
        assert row["id"] == 5  # noqa: PLR2004

    def test_row_at_invalid_index(self, qapp) -> None:  # type: ignore[no-untyped-def]
        assert self._model().row_at(self._model().index(0, 0)) is None

    def test_set_rows_notifies_views(self, qapp) -> None:  # type: ignore[no-untyped-def]
        model = self._model()
        reset = {"count": 0}
        model.modelAboutToBeReset.connect(lambda: reset.__setitem__("count", reset["count"] + 1))
        model.set_rows([{"id": 1}])
        assert reset["count"] == 1


class TestStatCard:
    """Dashboard tiles."""

    def test_shows_value_and_hint(self, qapp) -> None:  # type: ignore[no-untyped-def]
        card = StatCard("Healthy proxies", "usable now")
        card.set_value(3, "3 checked")
        assert card.value_text == "3"

    def test_placeholder_before_data(self, qapp) -> None:  # type: ignore[no-untyped-def]
        assert StatCard("x").value_text == "—"

    def test_accepts_strings_and_none(self, qapp) -> None:  # type: ignore[no-untyped-def]
        card = StatCard("x")
        card.set_value(None)
        assert card.value_text == "—"
        card.set_value("n/a")
        assert card.value_text == "n/a"


class TestSmallWidgets:
    """Buttons, headers and toolbars."""

    def test_button_variants(self, qapp) -> None:  # type: ignore[no-untyped-def]
        assert button("Go", primary=True).objectName() == "Primary"
        assert button("Go", danger=True).objectName() == "Danger"
        assert button("Go").objectName() == ""

    def test_page_header_returns_a_subtitle_label(self, qapp) -> None:  # type: ignore[no-untyped-def]
        container, subtitle = page_header("Title", "Sub")
        assert container is not None
        subtitle.setText("updated")
        assert subtitle.text() == "updated"

    def test_toolbar_accepts_widgets(self, qapp) -> None:  # type: ignore[no-untyped-def]
        assert toolbar(button("a"), button("b")) is not None


class TestTaskRunner:
    """Work happens off the GUI thread and reports through signals.

    These tests use ``qtbot.waitUntil`` rather than blocking on ``wait_for_done``: Qt delivers a
    signal from a worker thread through the receiving thread's event loop, so the callbacks only
    run while the loop is spinning. That is not a test artefact - it is exactly how the real
    application receives results.
    """

    def test_success_is_delivered(self, qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
        runner = TaskRunner()
        results: list[object] = []
        runner.submit(lambda: 6 * 7, on_success=results.append)
        qtbot.waitUntil(lambda: bool(results), timeout=3000)
        assert results == [42]

    def test_failure_is_delivered_as_an_api_error(self, qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
        runner = TaskRunner()
        errors: list[ApiError] = []

        def boom() -> None:
            raise NexusApiError(ApiError(code="nope", message="failed"))

        runner.submit(boom, on_error=errors.append)
        qtbot.waitUntil(lambda: bool(errors), timeout=3000)
        assert errors[0].code == "nope"

    def test_unexpected_exception_is_contained(self, qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
        # A bug in a view's callback must not kill the worker thread silently.
        runner = TaskRunner()
        errors: list[ApiError] = []

        def boom() -> None:
            raise ValueError("programmer error")

        runner.submit(boom, on_error=errors.append)
        qtbot.waitUntil(lambda: bool(errors), timeout=3000)
        assert errors[0].code == "client_error"

    def test_work_runs_off_the_gui_thread(self, qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
        runner = TaskRunner()
        gui_thread = threading.get_ident()
        seen: list[int] = []
        runner.submit(lambda: seen.append(threading.get_ident()))
        qtbot.waitUntil(lambda: bool(seen), timeout=3000)
        assert seen[0] != gui_thread

    def test_finished_always_fires(self, qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
        runner = TaskRunner()
        finished: list[bool] = []
        runner.submit(
            lambda: 1 / 0,
            on_error=lambda error: None,
            on_finished=lambda: finished.append(True),
        )
        qtbot.waitUntil(lambda: bool(finished), timeout=3000)
        assert finished == [True]

    def test_active_count_returns_to_zero(self, qapp, qtbot) -> None:  # type: ignore[no-untyped-def]
        runner = TaskRunner()
        runner.submit(lambda: 1)
        qtbot.waitUntil(lambda: runner.active_count == 0, timeout=3000)
        assert runner.active_count == 0

    def test_shutdown_is_safe_when_idle(self, qapp) -> None:  # type: ignore[no-untyped-def]
        TaskRunner().shutdown()

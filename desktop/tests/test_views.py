"""View tests.

Each view is constructed against the fake API, refreshed, and then inspected. ``qtbot.waitUntil``
spins the event loop so the worker threads' signals are delivered exactly as they are in the real
application - which is why these tests would catch a callback that never fires, not just a wrong
value.
"""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel

from app.main_window import MainWindow
from app.views import (
    DashboardView,
    InboxesView,
    JobsView,
    LogsView,
    ProxiesView,
    RegistrationView,
    SettingsView,
)
from tests.conftest import TEST_API_KEY


class TestDashboardView:
    """Cards and recent activity."""

    def test_refresh_populates_cards(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = DashboardView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.activity_model.rowCount() > 0, timeout=3000)

        assert view.healthy_proxies.value_text == "2"
        # unhealthy + expired: the dashboard counts what a user would call "failing".
        assert view.failing_proxies.value_text == "1"
        assert view.active_inboxes.value_text == "3"
        assert view.running_campaigns.value_text == "1"
        assert view.failed_jobs.value_text == "1"

    def test_activity_is_listed(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = DashboardView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.activity_model.rowCount() > 0, timeout=3000)
        assert view.activity_model.value(0, "action") == "proxy.import"

    def test_errors_are_reported_not_raised(self, client, runner, fake_api, qtbot) -> None:  # type: ignore[no-untyped-def]
        fake_api.routes.pop(("GET", "/api/v1/analytics/summary"))
        view = DashboardView(client, runner)
        messages: list[tuple[str, bool]] = []
        view.notify.connect(lambda text, is_error: messages.append((text, is_error)))

        view.refresh()
        qtbot.waitUntil(lambda: bool(messages), timeout=3000)
        assert messages[0][1] is True


class TestProxiesView:
    """Proxy table and actions."""

    def test_refresh_lists_proxies(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = ProxiesView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.model.rowCount() == 2, timeout=3000)
        assert view.model.value(0, "host") == "proxy1.example.test"

    def test_filter_is_sent_to_the_api(self, client, runner, fake_api, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = ProxiesView(client, runner)
        view.status_filter.setCurrentText("healthy")
        # Setting the combo triggers a refresh; wait for the call to be recorded.
        qtbot.waitUntil(lambda: ("GET", "/api/v1/proxies") in fake_api.calls, timeout=3000)
        assert ("GET", "/api/v1/proxies") in fake_api.calls

    def test_actions_without_a_selection_report_instead_of_failing(self, client, runner) -> None:  # type: ignore[no-untyped-def]
        view = ProxiesView(client, runner)
        messages: list[str] = []
        view.notify.connect(lambda text, _is_error: messages.append(text))
        view.check_selected()
        view.delete_selected()
        assert messages == ["Select a proxy first.", "Select a proxy first."]

    def test_check_selected_uses_the_single_endpoint(self, client, runner, fake_api, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = ProxiesView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.model.rowCount() > 0, timeout=3000)

        view.table.selectRow(0)
        view.check_selected()
        qtbot.waitUntil(lambda: ("POST", "/api/v1/proxies/1/check") in fake_api.calls, timeout=3000)

    def test_batch_check_uses_the_batch_endpoint(self, client, runner, fake_api, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = ProxiesView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.model.rowCount() == 2, timeout=3000)

        # Select two rows. ``selectRow`` clears the previous selection, so the selection flags are
        # used explicitly - the same thing a user does with ctrl-click.
        selection = view.table.selectionModel()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        selection.select(view.model.index(0, 0), flags)
        selection.select(view.model.index(1, 0), flags)
        assert len(view.selected_ids()) == 2  # noqa: PLR2004

        view.check_selected()
        qtbot.waitUntil(
            lambda: ("POST", "/api/v1/proxies/check-batch") in fake_api.calls, timeout=3000
        )

    def test_import_dialog_payload(self, qapp) -> None:  # type: ignore[no-untyped-def]
        from app.views.proxies import ProxyImportDialog

        dialog = ProxyImportDialog()
        dialog.text.setPlainText("p1.example.test:8080")
        content, fmt, name = dialog.payload
        assert content == "p1.example.test:8080"
        assert fmt == "auto"
        assert name == "Desktop import"


class TestInboxesView:
    """Inbox table and message pane."""

    def test_refresh_lists_inboxes(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = InboxesView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.inbox_model.rowCount() == 2, timeout=3000)
        assert view.inbox_model.value(0, "address") == "qa-inbox-1@example.test"

    def test_selecting_an_inbox_loads_messages(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = InboxesView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.inbox_model.rowCount() > 0, timeout=3000)

        view.inbox_table.selectRow(0)
        qtbot.waitUntil(lambda: view.message_model.rowCount() > 0, timeout=3000)
        assert view.message_model.value(0, "extracted_code") == "481920"
        assert "1 message" in view.messages_label.text()

    def test_selecting_a_message_shows_the_body(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = InboxesView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.inbox_model.rowCount() > 0, timeout=3000)
        view.inbox_table.selectRow(0)
        qtbot.waitUntil(lambda: view.message_model.rowCount() > 0, timeout=3000)

        view.message_table.selectRow(0)
        qtbot.waitUntil(lambda: "481920" in view.body.toPlainText(), timeout=3000)
        assert "HTML body present and deliberately not rendered" in view.body.toPlainText()

    def test_poll_reports_ingestion(self, client, runner, fake_api, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = InboxesView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.inbox_model.rowCount() > 0, timeout=3000)
        view.inbox_table.selectRow(0)

        messages: list[str] = []
        view.notify.connect(lambda text, _is_error: messages.append(text))
        view.poll_selected()
        qtbot.waitUntil(lambda: any("new" in message for message in messages), timeout=3000)


class TestRegistrationView:
    """Targets, campaigns and results."""

    def test_refresh_lists_targets_and_campaigns(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = RegistrationView(client, runner)
        view.refresh()
        qtbot.waitUntil(
            lambda: view.target_model.rowCount() > 0 and view.campaign_model.rowCount() > 0,
            timeout=3000,
        )
        assert view.target_model.value(0, "may_be_tested") is True

    def test_selecting_a_campaign_loads_results(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = RegistrationView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.campaign_model.rowCount() > 0, timeout=3000)

        view.campaign_table.selectRow(0)
        qtbot.waitUntil(lambda: view.result_model.rowCount() > 0, timeout=3000)
        assert view.result_model.value(0, "outcome") == "success"
        # Defects and controls are shown separately, never averaged together.
        assert "product defects: 0" in view.results_label.text()
        assert "controls encountered: 0" in view.results_label.text()

    def test_campaign_dialog_payload(self, qapp) -> None:  # type: ignore[no-untyped-def]
        from app.views.registration import CampaignDialog

        dialog = CampaignDialog("demo")
        payload = dialog.payload
        assert payload["count"] == 3  # noqa: PLR2004
        assert payload["concurrency"] == 1

    def test_adding_the_demo_target_posts_it(self, client, runner, fake_api, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = RegistrationView(client, runner)
        view.add_demo_target()
        qtbot.waitUntil(lambda: ("POST", "/api/v1/targets") in fake_api.calls, timeout=3000)


class TestJobsView:
    """Task monitor."""

    def test_refresh_lists_jobs_and_summarises(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = JobsView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.model.rowCount() == 2, timeout=3000)
        assert "completed" in view._subtitle.text()  # noqa: SLF001 - reading the summary line

    def test_selecting_a_job_shows_logs(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = JobsView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.model.rowCount() > 0, timeout=3000)

        view.table.selectRow(0)
        qtbot.waitUntil(lambda: bool(view.logs_view.toPlainText()), timeout=3000)
        text = view.logs_view.toPlainText()
        assert "checked 1/3" in text
        # The payload is shown as returned by the API - already redacted.
        assert "***REDACTED***" in text

    def test_cancel_requests_cancellation(self, client, runner, fake_api, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = JobsView(client, runner)
        view.refresh()
        qtbot.waitUntil(lambda: view.model.rowCount() > 0, timeout=3000)
        view.table.selectRow(0)

        view.cancel_selected()
        qtbot.waitUntil(
            lambda: ("POST", "/api/v1/jobs/job-1/cancel") in fake_api.calls, timeout=3000
        )


class TestSettingsView:
    """Read-only configuration display."""

    def test_forms_are_filled_from_the_api(self, client, runner, config, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = SettingsView(client, runner, config=config)
        view.refresh()
        qtbot.waitUntil(lambda: view.info_form.rowCount() > 0, timeout=3000)
        assert view.info_form.rowCount() == 8  # noqa: PLR2004
        assert view.limits_form.rowCount() == 6  # noqa: PLR2004
        assert view.client_form.rowCount() == 2  # noqa: PLR2004

    def test_probe_reports_readiness(self, client, runner, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = SettingsView(client, runner)
        messages: list[str] = []
        view.notify.connect(lambda text, _is_error: messages.append(text))
        view.probe()
        qtbot.waitUntil(lambda: bool(messages), timeout=3000)
        assert "ready=True" in messages[0]
        assert "database: ok" in messages[0]


class TestLogsView:
    """Client activity log."""

    def test_probe_appends_entries(self, client, runner, activity, qtbot) -> None:  # type: ignore[no-untyped-def]
        view = LogsView(client, runner, activity=activity)
        view.probe()
        qtbot.waitUntil(lambda: view.model.rowCount() >= 2, timeout=3000)
        assert activity.entries[-1].message == "ready=True"

    def test_clear_empties_the_log(self, client, runner, activity) -> None:  # type: ignore[no-untyped-def]
        view = LogsView(client, runner, activity=activity)
        view.append("info", "hello")
        assert view.model.rowCount() == 1
        view.clear()
        assert view.model.rowCount() == 0

    def test_shared_log_receives_appends(self, client, runner, activity) -> None:  # type: ignore[no-untyped-def]
        LogsView(client, runner, activity=activity)
        activity.info("from elsewhere")
        assert activity.entries[0].message == "from elsewhere"


class TestMainWindow:
    """Navigation, message routing and lifecycle."""

    def _window(self, client, runner, config, qtbot):  # type: ignore[no-untyped-def]
        window = MainWindow(client=client, config=config, runner=runner)
        qtbot.addWidget(window)
        return window

    def test_all_seven_pages_exist(self, client, runner, config, qtbot) -> None:  # type: ignore[no-untyped-def]
        window = self._window(client, runner, config, qtbot)
        assert len(window.views) == 7  # noqa: PLR2004
        assert window.nav.count() == 7  # noqa: PLR2004
        assert isinstance(window.views[0], DashboardView)
        assert isinstance(window.views[5], SettingsView)
        assert isinstance(window.views[6], LogsView)

    def test_switching_pages_refreshes_the_view(self, client, runner, config, qtbot) -> None:  # type: ignore[no-untyped-def]
        window = self._window(client, runner, config, qtbot)
        window.nav.setCurrentRow(1)  # Proxy Manager
        qtbot.waitUntil(lambda: window.views[1].model.rowCount() > 0, timeout=3000)
        assert window.stack.currentIndex() == 1

    def test_monitor_timer_runs_only_on_the_jobs_page(self, client, runner, config, qtbot) -> None:  # type: ignore[no-untyped-def]
        window = self._window(client, runner, config, qtbot)
        window.nav.setCurrentRow(0)
        assert not window._monitor_timer.isActive()  # noqa: SLF001

        window.nav.setCurrentRow(4)  # Task Monitor
        assert window._monitor_timer.isActive()  # noqa: SLF001

        window.nav.setCurrentRow(0)
        assert not window._monitor_timer.isActive()  # noqa: SLF001

    def test_view_messages_reach_the_status_bar_and_the_log(
        self, client, runner, config, qtbot
    ) -> None:  # type: ignore[no-untyped-def]
        window = self._window(client, runner, config, qtbot)
        window.views[1].notify.emit("Imported 3 proxies.", False)
        assert "Imported 3 proxies." in window.statusBar().currentMessage()
        assert window.activity.entries[-1].message == "Imported 3 proxies."

    def test_errors_are_prefixed(self, client, runner, config, qtbot) -> None:  # type: ignore[no-untyped-def]
        window = self._window(client, runner, config, qtbot)
        window.views[1].notify.emit("ssrf_blocked", True)
        assert window.statusBar().currentMessage().startswith("Error:")
        assert window.activity.entries[-1].level == "error"

    def test_startup_records_the_api_url(self, client, runner, config, qtbot) -> None:  # type: ignore[no-untyped-def]
        window = self._window(client, runner, config, qtbot)
        assert any(config.base_url in entry.message for entry in window.activity.entries)

    def test_api_key_is_not_shown_in_the_status_bar(self, client, runner, config, qtbot) -> None:  # type: ignore[no-untyped-def]
        window = self._window(client, runner, config, qtbot)
        assert TEST_API_KEY not in window.statusBar().currentMessage()

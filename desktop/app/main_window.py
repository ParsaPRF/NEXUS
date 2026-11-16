"""Main window: navigation rail, stacked views and the one status bar.

The window owns the cross-cutting behaviour so no view has to:

* it routes every view's ``notify`` signal into the status bar *and* the activity log, so the Logs
  page is never out of date;
* it drives the periodic refresh of the Task Monitor (only while that page is visible - polling a
  hidden page is wasted work and noise);
* it shuts the worker pool down and closes the HTTP client on exit, so no thread is left
  mid-request when the window disappears.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from app.activity import ActivityLog
from app.api_client import NexusClient
from app.config import DesktopConfig
from app.tasks import TaskRunner
from app.theme import apply_theme
from app.views import (
    DashboardView,
    InboxesView,
    JobsView,
    LogsView,
    ProxiesView,
    RegistrationView,
    SettingsView,
)
from app.views.base import BaseView

#: How often the Task Monitor refreshes while it is visible.
MONITOR_INTERVAL_MS = 2000


class MainWindow(QMainWindow):
    """The application window."""

    def __init__(
        self,
        *,
        client: NexusClient,
        config: DesktopConfig,
        runner: TaskRunner | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("NEXUS")
        self.resize(1280, 820)
        self._client = client
        self._config = config
        self._runner = runner or TaskRunner()
        self.activity = ActivityLog()

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(190)
        layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.views: list[BaseView] = []
        self._add_view(DashboardView, "Dashboard")
        self._add_view(ProxiesView, "Proxy Manager")
        self._add_view(InboxesView, "Email Manager")
        self._add_view(RegistrationView, "Registration Lab")
        self._add_view(JobsView, "Task Monitor")
        self._add_view(SettingsView, "Settings", config=config)
        self._add_view(LogsView, "Logs", activity=self.activity)

        # The timer is created BEFORE the current row is set: setting the row emits
        # currentRowChanged, which consults the timer. Creating it afterwards made the first page
        # change raise AttributeError inside the event loop.
        self._monitor_timer = QTimer(self)
        self._monitor_timer.setInterval(MONITOR_INTERVAL_MS)
        self._monitor_timer.timeout.connect(self._auto_refresh)

        self.nav.currentRowChanged.connect(self._page_changed)
        self.nav.setCurrentRow(0)

        self.setStatusBar(QStatusBar())
        self._show_connection_state()

        apply_theme(self)
        del layout

    # ------------------------------------------------------------------ construction

    def _add_view(self, view_class: type[BaseView], label: str, **kwargs: object) -> None:
        """Instantiate a view, add it to the stack and to the navigation rail."""
        view = view_class(self._client, self._runner, **kwargs)  # type: ignore[arg-type]
        view.notify.connect(self._on_notify)
        self.views.append(view)
        self.stack.addWidget(view)
        QListWidgetItem(label, self.nav)

    # ------------------------------------------------------------------ navigation

    def _page_changed(self, index: int) -> None:
        """Show a page and refresh it."""
        if index < 0:
            return
        self.stack.setCurrentIndex(index)
        view = self.views[index]
        view.refresh()
        self._update_monitor_timer(view)

    def _update_monitor_timer(self, view: BaseView) -> None:
        """Run the monitor timer only while the Task Monitor is on screen."""
        if isinstance(view, JobsView):
            self._monitor_timer.start()
        else:
            self._monitor_timer.stop()

    def _auto_refresh(self) -> None:
        """Refresh the visible monitor page."""
        current = self.stack.currentWidget()
        if isinstance(current, JobsView):
            current.auto_refresh()

    # ------------------------------------------------------------------ messages

    def _on_notify(self, message: str, is_error: bool) -> None:
        """Show a message in the status bar and record it in the activity log."""
        prefix = "Error: " if is_error else ""
        self.statusBar().showMessage(f"{prefix}{message}", 8000)
        self.activity.add("error" if is_error else "info", message)

    def _show_connection_state(self) -> None:
        """Report the API the client is pointed at, once, at start-up."""
        self.activity.info(f"Client started against {self._config.base_url}")
        self.statusBar().showMessage(f"NEXUS · {self._config.base_url}")

    # ------------------------------------------------------------------ lifecycle

    def refresh_all(self) -> None:
        """Refresh the current page (used after start-up)."""
        current = self.stack.currentWidget()
        if isinstance(current, BaseView):
            current.refresh()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        """Stop background work and close the connection pool."""
        self._monitor_timer.stop()
        self._runner.shutdown()
        self._client.close()
        super().closeEvent(event)


__all__ = ["MONITOR_INTERVAL_MS", "MainWindow"]

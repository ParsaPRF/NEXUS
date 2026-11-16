"""Logs view: the client-side activity log plus a backend readiness probe."""

from __future__ import annotations

from PySide6.QtWidgets import QTableView, QVBoxLayout, QWidget

from app.activity import ActivityLog
from app.formatting import shorten
from app.views.base import BaseView
from app.widgets import Column, DictTableModel, button, muted, page_header, toolbar

LOG_COLUMNS = [
    Column("time", "Time", 90),
    Column("level", "Level", 70, colorize=True),
    Column("message", "Message", 640, lambda value: shorten(value, 150)),
]


class LogsView(BaseView):
    """Shows what this client has done.

    The backend keeps its own logs (with request ids); this pane answers "what did I just click,
    and what came back", which is the question a status bar alone cannot answer.
    """

    title = "Logs"
    subtitle = "Client activity"

    def __init__(
        self,
        client,
        runner,
        *,
        activity: ActivityLog | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(client, runner, parent)
        self._activity = activity or ActivityLog()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header, self._subtitle = page_header("Logs", "Client activity in this session")
        layout.addWidget(header)

        self.probe_button = button("Check backend readiness")
        self.probe_button.clicked.connect(self.probe)
        self.clear_button = button("Clear", danger=True)
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(toolbar(self.probe_button, self.clear_button))
        layout.addWidget(
            muted(
                "Errors are reported here as well as in the status bar. The backend log is "
                "authoritative and is keyed by the request id shown in error messages."
            )
        )

        self.model = DictTableModel(LOG_COLUMNS)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        self._activity.appended.connect(self._on_appended)
        self._reload()

    # ------------------------------------------------------------------ activity log

    @property
    def activity(self) -> ActivityLog:
        """The shared activity log."""
        return self._activity

    def append(self, level: str, message: str) -> None:
        """Add an entry to the shared log."""
        self._activity.add(level, message)

    def _on_appended(self, entry) -> None:  # type: ignore[no-untyped-def]
        """Append one row without rebuilding the whole table."""
        self.model.set_rows(
            [
                *self.model.rows,
                {"time": entry.time_text, "level": entry.level, "message": entry.message},
            ]
        )

    def _reload(self) -> None:
        """Rebuild the table from the log."""
        self.model.set_rows(
            [
                {"time": entry.time_text, "level": entry.level, "message": entry.message}
                for entry in self._activity.entries
            ]
        )

    def clear(self) -> None:
        """Clear the log."""
        self._activity.clear()
        self._reload()

    # ------------------------------------------------------------------ probe

    def refresh(self) -> None:
        """Reload the table from the log."""
        self._reload()

    def probe(self) -> None:
        """Ask the backend how it is doing."""
        self.load(
            self.client.readiness,
            self._probed,
            busy=lambda busy: self.apply_busy(self.probe_button, busy),
        )

    def _probed(self, readiness: dict) -> None:
        """Log every dependency state, so a degraded one is visible rather than inferred."""
        for check in readiness.get("checks", []):
            level = "info" if check.get("state") == "ok" else "warning"
            self.append(level, f"{check.get('name')}: {check.get('state')} ({check.get('detail')})")
        self.append("info", f"ready={readiness.get('ready')}")

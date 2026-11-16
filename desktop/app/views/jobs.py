"""Task Monitor view: background jobs, progress, logs and cancellation."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.formatting import format_duration_ms, format_timestamp, shorten
from app.views.base import BaseView
from app.widgets import Column, DictTableModel, button, muted, page_header, toolbar

JOB_COLUMNS = [
    Column("public_id", "Job", 90, lambda value: shorten(value, 8)),
    Column("job_type", "Type", 150),
    Column("status", "Status", 100, colorize=True),
    Column("progress_current", "Done", 60),
    Column("progress_total", "Total", 60),
    Column("progress_percent", "Progress", 80, lambda value: f"{value or 0:.0f}%"),
    Column("attempts", "Tries", 50),
    Column("duration_ms", "Duration", 90, lambda value: format_duration_ms(value)),
    Column("queued_at", "Queued", 100, lambda value: format_timestamp(value)),
    Column("error_message", "Error", 200, lambda value: shorten(value, 40)),
]


class JobsView(BaseView):
    """The Task Monitor."""

    title = "Task Monitor"
    subtitle = "Background jobs"

    def __init__(self, client, runner, parent: QWidget | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(client, runner, parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header, self._subtitle = page_header("Task Monitor", "Loading…")
        layout.addWidget(header)

        self.status_filter = QComboBox()
        self.status_filter.addItems(
            ["all", "queued", "running", "completed", "failed", "cancelled"]
        )
        self.status_filter.currentTextChanged.connect(lambda _: self.refresh())

        self.cancel_button = button("Cancel job", danger=True)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.refresh_button = button("Refresh now")
        self.refresh_button.clicked.connect(self.refresh)

        layout.addWidget(toolbar(self.cancel_button, self.refresh_button, self.status_filter))

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        self.model = DictTableModel(JOB_COLUMNS)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self._job_selected)
        splitter.addWidget(self.table)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_label = muted("Select a job to see its log.")
        detail_layout.addWidget(self.detail_label)

        self.logs_view = QPlainTextEdit()
        self.logs_view.setReadOnly(True)
        self.logs_view.setPlaceholderText("Job log lines appear here.")
        detail_layout.addWidget(self.logs_view)
        splitter.addWidget(detail_panel)
        splitter.setSizes([340, 260])

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        """Load a page of jobs using the current filter."""
        status = self.status_filter.currentText()
        self.load(
            lambda: self.client.list_jobs(limit=100, status=None if status == "all" else status),
            self._apply_page,
        )

    def _apply_page(self, page: dict) -> None:
        """Display jobs and summarise them."""
        items = page.get("items", [])
        self.model.set_rows(items)
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("status"))
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
        self._subtitle.setText(summary or "no jobs")

    def selected_job_id(self) -> str | None:
        """Public id of the selected job."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.model.value(rows[0].row(), "public_id")

    # ------------------------------------------------------------------ actions

    def cancel_selected(self) -> None:
        """Ask the selected job to stop."""
        public_id = self.selected_job_id()
        if public_id is None:
            self.report("Select a job first.")
            return
        self.load(
            lambda: self.client.cancel_job(public_id),
            lambda result: self._cancelled(result),
            busy=lambda busy: self.apply_busy(self.cancel_button, busy),
        )

    def _cancelled(self, result: dict) -> None:
        """Report a cancellation request and refresh the row."""
        self.report(f"Cancellation requested for {str(result.get('public_id', ''))[:8]}")
        self.refresh()

    def _job_selected(self, *_args: object) -> None:
        """Load the selected job's detail and logs."""
        public_id = self.selected_job_id()
        if public_id is None:
            return
        self.load(lambda: self.client.get_job(public_id), self._apply_detail)

    def _apply_detail(self, job: dict) -> None:
        """Render the log pane.

        The payload is shown as JSON because it is already redacted by the backend - a credential
        passed as a job argument cannot be read back here.
        """
        self.detail_label.setText(
            f"{job.get('job_type')} · {job.get('status')} · "
            f"{job.get('progress_current', 0)}/{job.get('progress_total', 0)} · "
            f"{format_duration_ms(job.get('duration_ms'))}"
        )
        lines = [
            f"[{format_timestamp(entry.get('created_at'))}] {entry.get('level', 'info').upper()}: "
            f"{entry.get('message')}"
            for entry in job.get("logs", [])
        ]
        if job.get("error_message"):
            lines.append(f"error: {job['error_message']}")
        if job.get("payload"):
            lines.append("payload: " + json.dumps(job["payload"], ensure_ascii=False))
        self.logs_view.setPlainText("\n".join(lines) if lines else "(no log lines yet)")

    def auto_refresh(self) -> None:
        """Refresh quietly while the view is visible.

        Called by the main window's timer; it reuses :meth:`refresh` so there is one code path.
        """
        self.refresh()

"""Proxy Manager view: import, inspect, check, export."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.formatting import format_timestamp, yes_no
from app.views.base import BaseView
from app.widgets import Column, DictTableModel, button, muted, page_header, toolbar

PROXY_COLUMNS = [
    Column("id", "Id", 50),
    Column("protocol", "Protocol", 80),
    Column("host", "Host", 200),
    Column("port", "Port", 60),
    Column("username", "Account", 90),
    Column("status", "Status", 90, colorize=True),
    Column("latency_ms", "Latency", 80, lambda value: "" if value is None else f"{value} ms"),
    Column("reliability", "Reliability", 90, lambda value: "" if value is None else f"{value:.0%}"),
    Column("last_checked_at", "Last checked", 100, lambda value: format_timestamp(value)),
    Column("is_enabled", "Enabled", 70, yes_no),
]


class ProxyImportDialog(QDialog):
    """Paste or type a proxy list and import it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import proxies")
        self.resize(620, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(
            muted(
                "Paste a list (one entry per line) or CSV. Entries with a password are stored "
                "encrypted; without a configured key they are refused rather than stored in clear."
            )
        )
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("http://proxy.example.test:8080\nproxy2.example.test:3128")
        layout.addWidget(self.text, 1)

        form = QGridLayout()
        form.addWidget(QLabel("Format"), 0, 0)
        self.format_box = QComboBox()
        self.format_box.addItems(["auto", "txt", "csv"])
        form.addWidget(self.format_box, 0, 1)
        form.addWidget(QLabel("Batch name"), 0, 2)
        self.name_edit = QLineEdit("Desktop import")
        form.addWidget(self.name_edit, 0, 3)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def payload(self) -> tuple[str, str, str]:
        """Return ``(content, format, name)``."""
        return self.text.toPlainText(), self.format_box.currentText(), self.name_edit.text()


class ProxiesView(BaseView):
    """Table of proxies with the actions the service supports."""

    title = "Proxy Manager"
    subtitle = "Proxies you own or are authorized to use"

    def __init__(self, client, runner, parent: QWidget | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(client, runner, parent)
        self._last_total = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header, self._subtitle = page_header("Proxy Manager", "Loading…")
        layout.addWidget(header)

        self.status_filter = QComboBox()
        self.status_filter.addItems(["all", "unknown", "healthy", "unhealthy", "expired"])
        self.status_filter.currentTextChanged.connect(lambda _: self.refresh())

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search host or account")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.refresh)

        self.import_button = button("Import…", primary=True)
        self.import_button.clicked.connect(self.import_clicked)
        self.check_button = button("Check selected")
        self.check_button.clicked.connect(self.check_selected)
        self.check_unknown_button = button("Check unknown")
        self.check_unknown_button.clicked.connect(self.check_unknown)
        self.delete_button = button("Delete", danger=True)
        self.delete_button.clicked.connect(self.delete_selected)
        self.toggle_button = button("Enable/disable")
        self.toggle_button.clicked.connect(self.toggle_selected)
        self.export_button = button("Export CSV")
        self.export_button.clicked.connect(self.export_csv)
        self.refresh_button = button("Refresh")
        self.refresh_button.clicked.connect(self.refresh)

        layout.addWidget(
            toolbar(
                self.import_button,
                self.check_button,
                self.check_unknown_button,
                self.toggle_button,
                self.delete_button,
                self.export_button,
                self.refresh_button,
                self.status_filter,
                self.search,
            )
        )

        self.model = DictTableModel(PROXY_COLUMNS)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        """Load a page of proxies using the current filters."""
        status = self.status_filter.currentText()
        self.load(
            lambda: self.client.list_proxies(
                limit=200,
                status=None if status == "all" else status,
                search=self.search.text().strip() or None,
            ),
            self._apply_page,
        )

    def _apply_page(self, page: dict) -> None:
        """Display a page of proxies."""
        items = page.get("items", [])
        total = (page.get("meta") or {}).get("total", len(items))
        self._last_total = int(total)
        self.model.set_rows(items)
        self._subtitle.setText(f"showing {len(items)} of {total} proxies")

    # ------------------------------------------------------------------ selection

    def selected_ids(self) -> list[int]:
        """Ids of the selected rows."""
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [
            int(self.model.value(row, "id"))
            for row in sorted(rows)
            if self.model.value(row, "id") is not None
        ]

    # ------------------------------------------------------------------ actions

    def import_clicked(self) -> None:
        """Open the import dialog and submit the list."""
        dialog = ProxyImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        content, fmt, name = dialog.payload
        if not content.strip():
            self.report("Nothing to import.")
            return
        self.load(
            lambda: self.client.import_proxies(content, fmt=fmt, name=name),
            self._imported,
            busy=lambda busy: self.apply_busy(self.import_button, busy),
        )

    def _imported(self, result: dict) -> None:
        """Report the import outcome, including what was refused."""
        message = (
            f"Imported {result.get('accepted', 0)} proxies "
            f"({result.get('duplicates', 0)} duplicates, {result.get('rejected', 0)} rejected"
        )
        if result.get("credentials_refused"):
            message += f", {result['credentials_refused']} credentials refused"
        self.report(message + ").")
        self.refresh()

    def check_selected(self) -> None:
        """Check one proxy, or queue a batch when several are selected."""
        ids = self.selected_ids()
        if not ids:
            self.report("Select a proxy first.")
            return
        if len(ids) == 1:
            self.load(
                lambda: self.client.check_proxy(ids[0]),
                lambda result: self._checked_one(result),
                busy=lambda busy: self.apply_busy(self.check_button, busy),
            )
            return
        self.load(
            lambda: self.client.check_proxies(ids),
            lambda result: self._queued(result),
            busy=lambda busy: self.apply_busy(self.check_button, busy),
        )

    def _checked_one(self, result: dict) -> None:
        """Report a single check."""
        if result.get("ok"):
            self.report(f"Proxy {result['proxy_id']}: ok in {result.get('latency_ms')} ms")
        else:
            self.report(
                f"Proxy {result['proxy_id']}: {result.get('error_class')} ({result.get('status')})",
                error=True,
            )
        self.refresh()

    def check_unknown(self) -> None:
        """Queue a batch check for every proxy in the ``unknown`` state."""
        self.load(
            lambda: self.client.list_proxies(status="unknown", limit=200),
            self._check_unknown_page,
            busy=lambda busy: self.apply_busy(self.check_unknown_button, busy),
        )

    def _check_unknown_page(self, page: dict) -> None:
        """Queue a batch for the unknown proxies found."""
        ids = [int(item["id"]) for item in page.get("items", [])]
        if not ids:
            self.report("No unchecked proxies.")
            return
        self.load(lambda: self.client.check_proxies(ids), lambda result: self._queued(result))

    def _queued(self, result: dict) -> None:
        """Report a queued batch check."""
        self.report(
            f"Queued job {result.get('job_id', '')[:8]} for {result.get('proxy_count', 0)} proxies."
        )

    def toggle_selected(self) -> None:
        """Enable or disable the selected proxies."""
        ids = self.selected_ids()
        if not ids:
            self.report("Select a proxy first.")
            return
        enabled = not bool(self.model_value(ids[0], "is_enabled"))
        for proxy_id in ids:
            self.runner.submit(
                lambda proxy_id=proxy_id: self.client.set_proxy_enabled(proxy_id, enabled=enabled),
                on_error=self.report_error,
            )
        self.report(f"{'Enabled' if enabled else 'Disabled'} {len(ids)} proxies.")
        self.refresh()

    def model_value(self, proxy_id: int, key: str) -> object:
        """Look up a column value for a proxy id currently in the table."""
        for row in self.model.rows:
            if row.get("id") == proxy_id:
                return row.get(key)
        return None

    def delete_selected(self) -> None:
        """Delete the selected proxies and their history."""
        ids = self.selected_ids()
        if not ids:
            self.report("Select a proxy first.")
            return
        for proxy_id in ids:
            self.runner.submit(
                lambda proxy_id=proxy_id: self.client.delete_proxy(proxy_id),
                on_error=self.report_error,
            )
        self.report(f"Deleted {len(ids)} proxies.")
        self.refresh()

    def export_csv(self) -> None:
        """Export the current filter as CSV, reporting the row count rather than the path."""
        status = self.status_filter.currentText()
        self.load(
            lambda: self.client.export_proxies(
                fmt="csv", status=None if status == "all" else status, limit=10000
            ),
            self._exported,
            busy=lambda busy: self.apply_busy(self.export_button, busy),
        )

    def _exported(self, content: str) -> None:
        """Report an export (the caller decides where to save it)."""
        rows = max(0, len(content.splitlines()) - 1)
        self.report(f"Exported {rows} proxies as CSV.")

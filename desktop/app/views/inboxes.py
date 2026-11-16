"""Email Manager view: test inboxes and their messages."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QInputDialog,
    QLineEdit,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.formatting import format_timestamp, shorten
from app.views.base import BaseView
from app.widgets import Column, DictTableModel, button, muted, page_header, toolbar

INBOX_COLUMNS = [
    Column("address", "Address", 260),
    Column("label", "Label", 140),
    Column("status", "Status", 80, colorize=True),
    Column("message_count", "Messages", 80),
    Column("last_message_at", "Last mail", 100, lambda value: format_timestamp(value)),
    Column("expires_at", "Expires", 100, lambda value: format_timestamp(value)),
]

MESSAGE_COLUMNS = [
    Column("id", "Id", 50),
    Column("sender", "From", 200),
    Column("subject", "Subject", 260, lambda value: shorten(value, 60)),
    Column("received_at", "Received", 100, lambda value: format_timestamp(value)),
    Column("extracted_code", "Code", 90),
    Column(
        "extraction_confidence",
        "Confidence",
        90,
        lambda value: "" if value is None else f"{value:.0%}",
    ),
]


class InboxesView(BaseView):
    """Create disposable inboxes, poll them and read the mail that arrives."""

    title = "Email Manager"
    subtitle = "Disposable test inboxes on a reserved domain"

    def __init__(self, client, runner, parent: QWidget | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(client, runner, parent)
        self._selected_inbox: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header, self._subtitle = page_header("Email Manager", "Loading…")
        layout.addWidget(header)

        self.status_filter = QComboBox()
        self.status_filter.addItems(["all", "active", "expired", "archived"])
        self.status_filter.currentTextChanged.connect(lambda _: self.refresh())

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search address or label")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.refresh)

        self.new_button = button("New inbox", primary=True)
        self.new_button.clicked.connect(self.create_inbox)
        self.bulk_button = button("Create 10…")
        self.bulk_button.clicked.connect(self.bulk_create)
        self.poll_button = button("Poll selected")
        self.poll_button.clicked.connect(self.poll_selected)
        self.cleanup_button = button("Expire overdue")
        self.cleanup_button.clicked.connect(self.cleanup)
        self.delete_button = button("Delete", danger=True)
        self.delete_button.clicked.connect(self.delete_selected)
        self.export_button = button("Export messages")
        self.export_button.clicked.connect(self.export_messages)
        self.refresh_button = button("Refresh")
        self.refresh_button.clicked.connect(self.refresh)

        layout.addWidget(
            toolbar(
                self.new_button,
                self.bulk_button,
                self.poll_button,
                self.cleanup_button,
                self.delete_button,
                self.export_button,
                self.refresh_button,
                self.status_filter,
                self.search,
            )
        )

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        # -- inboxes -------------------------------------------------------------------
        inbox_panel = QWidget()
        inbox_layout = QVBoxLayout(inbox_panel)
        inbox_layout.setContentsMargins(0, 0, 0, 0)
        self.inbox_model = DictTableModel(INBOX_COLUMNS)
        self.inbox_table = QTableView()
        self.inbox_table.setModel(self.inbox_model)
        self.inbox_table.setAlternatingRowColors(True)
        self.inbox_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.inbox_table.verticalHeader().setVisible(False)
        self.inbox_table.horizontalHeader().setStretchLastSection(True)
        self.inbox_table.selectionModel().selectionChanged.connect(self._inbox_selected)
        inbox_layout.addWidget(self.inbox_table)
        splitter.addWidget(inbox_panel)

        # -- messages and detail --------------------------------------------------------
        messages_panel = QWidget()
        messages_layout = QVBoxLayout(messages_panel)
        messages_layout.setContentsMargins(0, 0, 0, 0)
        self.messages_label = muted("Select an inbox to see its messages.")
        messages_layout.addWidget(self.messages_label)

        self.message_model = DictTableModel(MESSAGE_COLUMNS)
        self.message_table = QTableView()
        self.message_table.setModel(self.message_model)
        self.message_table.setAlternatingRowColors(True)
        self.message_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.message_table.verticalHeader().setVisible(False)
        self.message_table.horizontalHeader().setStretchLastSection(True)
        self.message_table.selectionModel().selectionChanged.connect(self._message_selected)
        messages_layout.addWidget(self.message_table, 1)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setPlaceholderText(
            "Message body (text only - HTML is never rendered: it is untrusted input)."
        )
        self.body.setMaximumHeight(140)
        messages_layout.addWidget(self.body)
        splitter.addWidget(messages_panel)
        splitter.setSizes([320, 380])

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        """Load a page of inboxes."""
        status = self.status_filter.currentText()
        self.load(
            lambda: self.client.list_inboxes(
                limit=200,
                status=None if status == "all" else status,
                search=self.search.text().strip() or None,
            ),
            self._apply_page,
        )

    def _apply_page(self, page: dict) -> None:
        """Display the inboxes."""
        items = page.get("items", [])
        total = (page.get("meta") or {}).get("total", len(items))
        self.inbox_model.set_rows(items)
        self._subtitle.setText(f"showing {len(items)} of {total} inboxes")

    def selected_inbox_id(self) -> str | None:
        """Public id of the selected inbox."""
        rows = self.inbox_table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.inbox_model.value(rows[0].row(), "public_id")

    # ------------------------------------------------------------------ inbox actions

    def create_inbox(self) -> None:
        """Create one inbox, optionally with a label."""
        label, accepted = QInputDialog.getText(self, "New inbox", "Label (optional)")
        if not accepted:
            return
        self.load(
            lambda: self.client.create_inbox(label=label or None),
            self._created,
            busy=lambda busy: self.apply_busy(self.new_button, busy),
        )

    def _created(self, inbox: dict) -> None:
        """Report a newly created inbox."""
        self.report(f"Created {inbox.get('address')}")
        self.refresh()

    def bulk_create(self) -> None:
        """Create several inboxes at once."""
        count, accepted = QInputDialog.getInt(self, "Bulk create", "How many?", 10, 1, 100)
        if not accepted:
            return
        self.load(
            lambda: self.client.bulk_create_inboxes(count, label_prefix="desktop"),
            lambda result: self._bulk_created(result),
            busy=lambda busy: self.apply_busy(self.bulk_button, busy),
        )

    def _bulk_created(self, result: dict) -> None:
        """Report a bulk creation (the cap is the backend's; a refusal surfaces as an error)."""
        self.report(f"Created {result.get('created', 0)} inboxes.")
        self.refresh()

    def poll_selected(self) -> None:
        """Ingest new mail for the selected inbox."""
        public_id = self.selected_inbox_id()
        if public_id is None:
            self.report("Select an inbox first.")
            return
        self.load(
            lambda: self.client.poll_inbox(public_id),
            self._polled,
            busy=lambda busy: self.apply_busy(self.poll_button, busy),
        )

    def _polled(self, result: dict) -> None:
        """Report ingestion and reload the messages.

        The poll response identifies the inbox by address, so the messages are reloaded for the
        inbox that is currently selected rather than by parsing the address back into an id.
        """
        self.report(
            f"{result.get('inbox')}: {result.get('ingested', 0)} new, "
            f"{result.get('already_known', 0)} already stored."
        )
        self.refresh()
        self._load_messages(self._selected_inbox)

    def cleanup(self) -> None:
        """Expire overdue inboxes."""
        self.load(self.client.cleanup_inboxes, self._cleaned)

    def _cleaned(self, result: dict) -> None:
        """Report the sweep."""
        self.report(f"Expired {result.get('expired', 0)} inboxes.")
        self.refresh()

    def delete_selected(self) -> None:
        """Delete the selected inbox and its messages."""
        public_id = self.selected_inbox_id()
        if public_id is None:
            self.report("Select an inbox first.")
            return
        self.runner.submit(
            lambda: self.client.delete_inbox(public_id),
            on_success=lambda _: self._deleted(),
            on_error=self.report_error,
        )

    def _deleted(self) -> None:
        """Report a deletion and clear the message pane."""
        self.report("Inbox deleted.")
        self._selected_inbox = None
        self.message_model.set_rows([])
        self.body.clear()
        self.refresh()

    def export_messages(self) -> None:
        """Export the selected inbox's messages as CSV."""
        public_id = self.selected_inbox_id()
        if public_id is None:
            self.report("Select an inbox first.")
            return
        self.load(
            lambda: self.client.export_messages(public_id, fmt="csv"),
            lambda content: self.report(
                f"Exported {max(0, len(content.splitlines()) - 1)} messages."
            ),
            busy=lambda busy: self.apply_busy(self.export_button, busy),
        )

    # ------------------------------------------------------------------ messages

    def _inbox_selected(self, *_args: object) -> None:
        """Load messages for the newly selected inbox."""
        public_id = self.selected_inbox_id()
        if public_id is None:
            return
        self._selected_inbox = public_id
        self._load_messages(public_id)

    def _load_messages(self, public_id: str | None) -> None:
        """Fetch the messages of one inbox."""
        if not public_id:
            return
        self.load(
            lambda: self.client.list_messages(public_id, limit=100),
            self._apply_messages,
        )

    def _apply_messages(self, page: dict) -> None:
        """Display the messages."""
        items = page.get("items", [])
        total = (page.get("meta") or {}).get("total", len(items))
        self.message_model.set_rows(items)
        self.messages_label.setText(f"{total} message(s) in this inbox")
        self.body.clear()

    def _message_selected(self, *_args: object) -> None:
        """Load the full body of the selected message."""
        rows = self.message_table.selectionModel().selectedRows()
        if not rows:
            return
        message_id = self.message_model.value(rows[0].row(), "id")
        if message_id is None:
            return
        self.load(lambda: self.client.get_message(int(message_id)), self._apply_message)

    def _apply_message(self, message: dict) -> None:
        """Show the message body (text only)."""
        header = (
            f"From: {message.get('sender')}\n"
            f"Subject: {message.get('subject')}\n"
            f"Code: {message.get('extracted_code') or '(none extracted)'}\n"
            f"{'-' * 40}\n"
        )
        body = message.get("body_text") or "(no text body)"
        if message.get("body_html"):
            body += "\n\n[HTML body present and deliberately not rendered]"
        self.body.setPlainText(header + body)

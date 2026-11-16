"""Registration Lab view: targets, campaigns and results."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.formatting import format_duration_ms, format_timestamp, shorten, yes_no
from app.views.base import BaseView
from app.widgets import Column, DictTableModel, button, muted, page_header, toolbar

TARGET_COLUMNS = [
    Column("id", "Id", 40),
    Column("name", "Name", 180),
    Column("base_url", "Base URL", 200),
    Column("kind", "Kind", 60),
    Column("is_authorized", "Authorized", 90, yes_no),
    Column("may_be_tested", "Usable", 70, yes_no),
    Column("authorization_note", "Note", 240, lambda value: shorten(value, 60)),
]

CAMPAIGN_COLUMNS = [
    Column("public_id", "Id", 90, lambda value: shorten(value, 8)),
    Column("name", "Name", 180),
    Column("status", "Status", 130, colorize=True),
    Column("total_tests", "Total", 60),
    Column("completed_tests", "Done", 60),
    Column("succeeded_tests", "Passed", 70, colorize=False),
    Column("failed_tests", "Failed", 70),
    Column("progress_percent", "Progress", 80, lambda value: f"{value or 0:.0f}%"),
    Column("stop_reason", "Stop reason", 200, lambda value: shorten(value, 60)),
]

RESULT_COLUMNS = [
    Column("email", "Identity", 240),
    Column("status", "Status", 120, colorize=True),
    Column("outcome", "Outcome", 150, colorize=True),
    Column("verification_code", "Code", 80),
    Column(
        "code_extraction_confidence",
        "Confidence",
        90,
        lambda value: "" if value is None else f"{value:.0%}",
    ),
    Column("account_status", "Account", 90, colorize=True),
    Column("duration_ms", "Duration", 80, lambda value: format_duration_ms(value)),
]


class CampaignDialog(QDialog):
    """Collect the parameters for a new campaign."""

    def __init__(self, target_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"New campaign against {target_name}")
        self.resize(460, 260)

        layout = QVBoxLayout(self)
        layout.addWidget(
            muted(
                "Identities are generated on a domain this instance hosts. Rate and concurrency "
                "are fixed; a target control (429 / CAPTCHA / denial) stops the run."
            )
        )

        form = QFormLayout()
        self.name_edit = QLineEdit("Desktop campaign")
        form.addRow("Name", self.name_edit)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 50)
        self.count_spin.setValue(3)
        form.addRow("Identities", self.count_spin)

        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 5)
        self.concurrency_spin.setValue(1)
        form.addRow("Concurrency", self.concurrency_spin)

        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(1, 120)
        self.rate_spin.setValue(30)
        self.rate_spin.setSuffix(" per minute")
        form.addRow("Rate", self.rate_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 300)
        self.timeout_spin.setValue(30)
        self.timeout_spin.setSuffix(" s")
        form.addRow("Email timeout", self.timeout_spin)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def payload(self) -> dict[str, object]:
        """Return the campaign payload."""
        return {
            "name": self.name_edit.text() or "Desktop campaign",
            "count": self.count_spin.value(),
            "concurrency": self.concurrency_spin.value(),
            "rate_limit_per_minute": self.rate_spin.value(),
            "email_timeout_seconds": self.timeout_spin.value(),
        }


class RegistrationView(BaseView):
    """Targets, campaigns and per-test results."""

    title = "Registration Lab"
    subtitle = "Authorized registration testing"

    def __init__(self, client, runner, parent: QWidget | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(client, runner, parent)
        self._selected_campaign: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header, self._subtitle = page_header("Registration Lab", "Loading…")
        layout.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)

        # -- targets -------------------------------------------------------------------
        targets_panel = QWidget()
        targets_layout = QVBoxLayout(targets_panel)
        targets_layout.setContentsMargins(0, 0, 0, 0)
        targets_layout.addWidget(
            muted("Targets - both gates (authorization note and policy) apply")
        )

        self.add_demo_button = button("Add bundled demo target", primary=True)
        self.add_demo_button.clicked.connect(self.add_demo_target)
        self.authorize_button = button("Authorize selected…")
        self.authorize_button.clicked.connect(self.authorize_selected)
        self.refresh_targets_button = button("Refresh targets")
        self.refresh_targets_button.clicked.connect(self.refresh_targets)
        targets_layout.addWidget(
            toolbar(self.add_demo_button, self.authorize_button, self.refresh_targets_button)
        )

        self.target_model = DictTableModel(TARGET_COLUMNS)
        self.target_table = QTableView()
        self.target_table.setModel(self.target_model)
        self.target_table.setAlternatingRowColors(True)
        self.target_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.target_table.verticalHeader().setVisible(False)
        self.target_table.horizontalHeader().setStretchLastSection(True)
        targets_layout.addWidget(self.target_table)
        splitter.addWidget(targets_panel)

        # -- campaigns -----------------------------------------------------------------
        campaigns_panel = QWidget()
        campaigns_layout = QVBoxLayout(campaigns_panel)
        campaigns_layout.setContentsMargins(0, 0, 0, 0)

        self.new_campaign_button = button("New campaign…", primary=True)
        self.new_campaign_button.clicked.connect(self.create_campaign)
        self.cancel_campaign_button = button("Cancel campaign", danger=True)
        self.cancel_campaign_button.clicked.connect(self.cancel_campaign)
        self.refresh_campaigns_button = button("Refresh campaigns")
        self.refresh_campaigns_button.clicked.connect(self.refresh_campaigns)
        campaigns_layout.addWidget(
            toolbar(
                self.new_campaign_button, self.cancel_campaign_button, self.refresh_campaigns_button
            )
        )

        self.campaign_model = DictTableModel(CAMPAIGN_COLUMNS)
        self.campaign_table = QTableView()
        self.campaign_table.setModel(self.campaign_model)
        self.campaign_table.setAlternatingRowColors(True)
        self.campaign_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.campaign_table.verticalHeader().setVisible(False)
        self.campaign_table.horizontalHeader().setStretchLastSection(True)
        self.campaign_table.selectionModel().selectionChanged.connect(self._campaign_selected)
        campaigns_layout.addWidget(self.campaign_table)
        splitter.addWidget(campaigns_panel)

        # -- results -------------------------------------------------------------------
        results_panel = QWidget()
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_label = muted("Select a campaign to see per-test results.")
        results_layout.addWidget(self.results_label)

        self.result_model = DictTableModel(RESULT_COLUMNS)
        self.result_table = QTableView()
        self.result_table.setModel(self.result_model)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        results_layout.addWidget(self.result_table)
        splitter.addWidget(results_panel)
        splitter.setSizes([220, 240, 240])

    # ------------------------------------------------------------------ targets

    def refresh(self) -> None:
        """Load targets and campaigns."""
        self.refresh_targets()
        self.refresh_campaigns()

    def refresh_targets(self) -> None:
        """Load the target list."""
        self.load(self.client.list_targets, self._apply_targets)

    def _apply_targets(self, targets: list[dict]) -> None:
        """Display targets."""
        self.target_model.set_rows(targets)
        usable = sum(1 for target in targets if target.get("may_be_tested"))
        self._subtitle.setText(f"{len(targets)} targets, {usable} usable")

    def selected_target_id(self) -> int | None:
        """Id of the selected target."""
        rows = self.target_table.selectionModel().selectedRows()
        if not rows:
            return None
        value = self.target_model.value(rows[0].row(), "id")
        return int(value) if value is not None else None

    def add_demo_target(self) -> None:
        """Register the bundled demonstration application."""
        self.load(
            lambda: self.client.create_target(
                name="Bundled demo application",
                base_url="http://127.0.0.1:9000",
                kind="demo",
                adapter_name="demo_api",
                is_authorized=True,
                authorization_note="Bundled demonstration application owned by this repository.",
            ),
            lambda target: self._target_added(target),
            busy=lambda busy: self.apply_busy(self.add_demo_button, busy),
        )

    def _target_added(self, target: dict) -> None:
        """Report a newly registered target."""
        self.report(f"Registered target {target.get('name')}")
        self.refresh_targets()

    def authorize_selected(self) -> None:
        """Record an authorization note for the selected target."""
        target_id = self.selected_target_id()
        if target_id is None:
            self.report("Select a target first.")
            return
        note, accepted = QInputDialog.getText(
            self,
            "Authorize target",
            "Who authorized testing this target, and when?",
        )
        if not accepted or not note.strip():
            return
        self.load(
            lambda: self.client.authorize_target(target_id, note),
            lambda target: self._authorized(target),
        )

    def _authorized(self, target: dict) -> None:
        """Report an authorization."""
        self.report(f"Authorized {target.get('name')}")
        self.refresh_targets()

    # ------------------------------------------------------------------ campaigns

    def refresh_campaigns(self) -> None:
        """Load the campaign list."""
        self.load(self.client.list_campaigns, self._apply_campaigns)

    def _apply_campaigns(self, page: dict) -> None:
        """Display campaigns."""
        self.campaign_model.set_rows(page.get("items", []))

    def selected_campaign(self) -> dict | None:
        """The selected campaign row."""
        rows = self.campaign_table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.campaign_model.row_at(rows[0])

    def create_campaign(self) -> None:
        """Ask for parameters, then create and queue a campaign."""
        target_id = self.selected_target_id()
        if target_id is None:
            self.report("Select a target first.")
            return
        target_row = None
        rows = self.target_table.selectionModel().selectedRows()
        if rows:
            target_row = self.target_model.row_at(rows[0])
        dialog = CampaignDialog((target_row or {}).get("name", "target"), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload
        self.load(
            lambda: self.client.create_campaign(target_id=target_id, **payload),
            self._campaign_created,
            busy=lambda busy: self.apply_busy(self.new_campaign_button, busy),
        )

    def _campaign_created(self, result: dict) -> None:
        """Report a queued campaign."""
        campaign = result.get("campaign", {})
        self.report(
            f"Queued campaign {campaign.get('public_id', '')[:8]} "
            f"({campaign.get('total_tests', 0)} identities)."
        )
        self.refresh_campaigns()

    def cancel_campaign(self) -> None:
        """Cancel the selected campaign."""
        campaign = self.selected_campaign()
        if campaign is None:
            self.report("Select a campaign first.")
            return
        self.load(
            lambda: self.client.cancel_campaign(campaign["public_id"]),
            lambda result: self._campaign_cancelled(result),
        )

    def _campaign_cancelled(self, campaign: dict) -> None:
        """Report a cancelled campaign."""
        self.report(f"Campaign {campaign.get('status')}")
        self.refresh_campaigns()

    def _campaign_selected(self, *_args: object) -> None:
        """Load results for the selected campaign."""
        campaign = self.selected_campaign()
        if campaign is None:
            return
        self._selected_campaign = campaign["public_id"]
        self.load(
            lambda: self.client.campaign_results(campaign["public_id"]),
            self._apply_results,
        )

    def _apply_results(self, page: dict) -> None:
        """Display results, keeping defects and controls visibly separate."""
        summary = page.get("summary", {})
        self.result_model.set_rows(page.get("items", []))
        self.results_label.setText(
            f"success: {summary.get('success', 0)} · "
            f"product defects: {summary.get('product_defects', 0)} · "
            f"controls encountered: {summary.get('controls_encountered', 0)}"
        )

    def refresh_results(self) -> None:
        """Reload results for the currently selected campaign (used by the auto-refresh timer)."""
        if self._selected_campaign:
            self.load(
                lambda: self.client.campaign_results(self._selected_campaign or ""),
                self._apply_results,
            )


__all__ = ["RegistrationView", "format_timestamp", "QPlainTextEdit"]

"""Dashboard: the state of the whole instance at a glance."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QGridLayout,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.formatting import format_timestamp, shorten
from app.views.base import BaseView
from app.widgets import Column, DictTableModel, StatCard, muted, page_header


class DashboardView(BaseView):
    """Widgets the brief asks for: proxy health, active inboxes, running campaigns, activity."""

    title = "Dashboard"
    subtitle = "Instance overview"

    def __init__(self, client, runner, parent: QWidget | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(client, runner, parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)

        header, self._subtitle = page_header("Dashboard", "Loading…")
        layout.addWidget(header)

        # -- stat cards ---------------------------------------------------------------
        cards = QGridLayout()
        cards.setHorizontalSpacing(12)
        cards.setVerticalSpacing(12)
        self.healthy_proxies = StatCard("Healthy proxies", "usable right now")
        self.failing_proxies = StatCard("Failing proxies", "unhealthy or expired")
        self.active_inboxes = StatCard("Active test inboxes", "able to receive mail")
        self.running_campaigns = StatCard("Running campaigns", "registration runs in progress")
        self.running_jobs = StatCard("Running jobs", "background work")
        self.failed_jobs = StatCard("Failed jobs", "needs attention")

        card_list = [
            self.healthy_proxies,
            self.failing_proxies,
            self.active_inboxes,
            self.running_campaigns,
            self.running_jobs,
            self.failed_jobs,
        ]
        for index, card in enumerate(card_list):
            cards.addWidget(card, index // 3, index % 3)
        layout.addLayout(cards)

        # -- recent activity -----------------------------------------------------------
        layout.addWidget(muted("Recent activity (from the audit trail)"))
        self.activity_model = DictTableModel(
            [
                Column("created_at", "When", 90, lambda value: format_timestamp(value)),
                Column("action", "Action", 200),
                Column("entity_type", "Entity", 140),
                Column("entity_id", "Id", 90),
                Column("outcome", "Outcome", 100, colorize=True),
            ]
        )
        self.activity_table = QTableView()
        self.activity_table.setModel(self.activity_model)
        self.activity_table.setAlternatingRowColors(True)
        self.activity_table.horizontalHeader().setStretchLastSection(True)
        self.activity_table.verticalHeader().setVisible(False)
        self.activity_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        layout.addWidget(self.activity_table, 1)

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        """Load the analytics summary."""
        self.load(self.client.analytics_summary, self._apply_summary)

    def _apply_summary(self, summary: dict) -> None:
        """Push the summary into the cards and the activity table."""
        proxies = summary.get("proxies", {})
        jobs = summary.get("jobs", {})
        campaigns = summary.get("campaigns", {})

        failing = int(proxies.get("unhealthy", 0)) + int(proxies.get("expired", 0))
        self.healthy_proxies.set_value(summary.get("healthy_proxies", 0), "usable right now")
        self.failing_proxies.set_value(failing, "unhealthy or expired")
        self.failing_proxies.set_color("danger" if failing else "text")
        self.active_inboxes.set_value(summary.get("active_inboxes", 0), "able to receive mail")
        self.running_campaigns.set_value(
            summary.get("running_campaigns", 0), f"{summary.get('total_campaigns', 0)} total"
        )
        self.running_jobs.set_value(
            summary.get("running_jobs", 0), f"{summary.get('total_jobs', 0)} total"
        )
        failed = int(summary.get("failed_jobs", 0))
        self.failed_jobs.set_value(failed, "needs attention")
        self.failed_jobs.set_color("danger" if failed else "text")

        activity = [
            {**item, "entity_type": shorten(item.get("entity_type") or "", 24)}
            for item in summary.get("recent_activity", [])
        ]
        self.activity_model.set_rows(activity)
        self._subtitle.setText(
            f"{summary.get('total_proxies', 0)} proxies · "
            f"{summary.get('total_inboxes', 0)} inboxes · "
            f"{summary.get('total_campaigns', 0)} campaigns · "
            f"{summary.get('total_jobs', 0)} jobs"
        )
        del jobs, campaigns

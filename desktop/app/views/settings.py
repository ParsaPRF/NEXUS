"""Settings view: what the backend is, and the limits currently in force.

Read-only by design. Configuration lives in the environment (backend) and in
``NEXUS_DESKTOP_*`` variables (this client); a settings screen that could rewrite a running
backend's safety limits would be a way to weaken them from a GUI.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.views.base import BaseView
from app.widgets import button, muted, page_header, toolbar


class SettingsView(BaseView):
    """Instance information, safety limits and connection details."""

    title = "Settings"
    subtitle = "Read-only view of the backend configuration"

    def __init__(self, client, runner, *, config=None, parent: QWidget | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(client, runner, parent)
        self._config = config

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header, self._subtitle = page_header("Settings", "Loading…")
        layout.addWidget(header)

        self.probe_button = button("Test connection", primary=True)
        self.probe_button.clicked.connect(self.probe)
        self.refresh_button = button("Reload")
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(toolbar(self.probe_button, self.refresh_button))

        self.info_panel, self.info_form = self._build_form("Backend")
        layout.addWidget(self.info_panel)
        self.limits_panel, self.limits_form = self._build_form("Safety limits in force")
        layout.addWidget(self.limits_panel)
        self.client_panel, self.client_form = self._build_form("This client")
        layout.addWidget(self.client_panel)

        layout.addWidget(
            muted(
                "Safety limits are configuration, not preferences: a target must be authorized and "
                "allowlisted, controls stop a run, and private addresses stay blocked unless the "
                "operator opted in on the backend."
            )
        )
        layout.addStretch(1)

    def _build_form(self, title: str) -> tuple[QFrame, QFormLayout]:
        """Create a titled panel containing an empty, read-only form.

        The panel widget is returned *and stored on the view*. Returning only the layout and
        reaching for ``parentWidget()`` looks tidier but is a bug: Qt owns the C++ object, Python
        owns the reference, and once the last Python reference goes the widget is destroyed - which
        is exactly what happened here ("Internal C++ object already deleted").
        """
        panel = QFrame()
        panel.setObjectName("Panel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.addWidget(QLabel(title))

        form = QFormLayout()
        outer.addLayout(form)
        return panel, form

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        """Load instance info and limits."""
        self.load(self.client.instance_info, self._apply_info)
        self.load(self.client.safety_limits, self._apply_limits)
        self._apply_client_config()

    def _apply_info(self, info: dict) -> None:
        """Show instance facts."""
        self._fill(
            self.info_form,
            {
                "Application": info.get("app"),
                "Version": info.get("version"),
                "Environment": info.get("environment"),
                "Profile": info.get("profile"),
                "Authentication enabled": info.get("auth_enabled"),
                "Database dialect": info.get("database_dialect"),
                "Mail backend": info.get("mail_backend"),
                "Job backend": info.get("job_backend"),
            },
        )
        self._subtitle.setText(
            f"{info.get('app')} {info.get('version')} · {info.get('environment')} · "
            f"{info.get('profile')}"
        )

    def _apply_limits(self, limits: dict) -> None:
        """Show the active safety limits."""
        self._fill(
            self.limits_form,
            {
                "Private targets allowed": limits.get("allow_private_targets"),
                "Allowed target hosts": ", ".join(limits.get("allowed_target_hosts") or [])
                or "(none)",
                "HTTP timeout": f"{limits.get('http_timeout_seconds')} s",
                "Max concurrent checks": limits.get("max_concurrent_checks"),
                "Max response size": f"{limits.get('max_response_bytes')} bytes",
                "Registration note": limits.get("registration_disabled_reason")
                or "(external targets allowed)",
            },
        )

    def _apply_client_config(self) -> None:
        """Show how this client is pointed at the backend."""
        base_url = getattr(self._config, "base_url", "?")
        has_key = bool(getattr(self._config, "api_key", ""))
        self._fill(
            self.client_form,
            {
                "API URL": base_url,
                "API key": "configured" if has_key else "not set (the backend must allow that)",
            },
        )

    def _fill(self, form: QFormLayout, values: dict[str, object]) -> None:
        """Replace a form's rows with read-only fields."""
        while form.rowCount():
            form.removeRow(0)
        for label, value in values.items():
            field = QLineEdit("" if value is None else str(value))
            field.setReadOnly(True)
            form.addRow(label, field)

    def probe(self) -> None:
        """Check that the API answers, reporting readiness detail."""
        self.load(
            self.client.readiness,
            self._probed,
            busy=lambda busy: self.apply_busy(self.probe_button, busy),
        )

    def _probed(self, readiness: dict) -> None:
        """Report the probe result."""
        states = ", ".join(
            f"{check.get('name')}: {check.get('state')}" for check in readiness.get("checks", [])
        )
        self.report(f"ready={readiness.get('ready')} · {states}")

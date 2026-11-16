"""Views for the NEXUS desktop client."""

from app.views.dashboard import DashboardView
from app.views.inboxes import InboxesView
from app.views.jobs import JobsView
from app.views.logs import LogsView
from app.views.proxies import ProxiesView
from app.views.registration import RegistrationView
from app.views.settings import SettingsView

__all__ = [
    "DashboardView",
    "InboxesView",
    "JobsView",
    "LogsView",
    "ProxiesView",
    "RegistrationView",
    "SettingsView",
]

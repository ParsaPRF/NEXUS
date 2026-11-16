"""Shared fixtures for the desktop tests.

The Qt platform is forced to ``offscreen`` **before** any Qt module is imported: this machine has
no display, and the offscreen plugin is what makes the widget tests runnable in CI as well.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Qt warns loudly when no fonts are installed; the tests do not care about glyphs.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.activity import ActivityLog  # noqa: E402
from app.api_client import NexusClient  # noqa: E402
from app.config import DesktopConfig  # noqa: E402
from app.tasks import TaskRunner  # noqa: E402
from tests.fake_api import FakeApi  # noqa: E402

TEST_API_KEY = "desktop-test-key-0123456789"


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """One ``QApplication`` for the whole session.

    Qt allows only one per process, and pytest-qt's ``qapp`` fixture does the same thing; this one
    exists so non-``qtbot`` tests can still construct widgets.
    """
    return QApplication.instance() or QApplication([])


@pytest.fixture
def config() -> DesktopConfig:
    """A configuration pointing at a fake host."""
    return DesktopConfig(base_url="http://nexus.test", api_key=TEST_API_KEY, timeout_seconds=5.0)


@pytest.fixture
def fake_api() -> FakeApi:
    """The canned API."""
    return FakeApi()


@pytest.fixture
def client(config: DesktopConfig, fake_api: FakeApi) -> NexusClient:
    """A client wired to the fake API."""
    api_client = NexusClient(config, transport=fake_api.transport())
    yield api_client
    api_client.close()


@pytest.fixture
def runner(qapp: QApplication) -> TaskRunner:
    """A small thread pool for view tests."""
    task_runner = TaskRunner(max_threads=2)
    yield task_runner
    task_runner.shutdown()


@pytest.fixture
def activity() -> ActivityLog:
    """A fresh activity log."""
    return ActivityLog()

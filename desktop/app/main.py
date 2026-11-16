"""Entry point for the NEXUS desktop client.

    uv run python -m app.main

Environment:

* ``NEXUS_DESKTOP_API_URL`` - where the backend is (default ``http://127.0.0.1:8000``)
* ``NEXUS_DESKTOP_API_KEY`` - the ``X-API-Key`` value (falls back to ``NEXUS_API_KEY``)
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from app.api_client import NexusClient
from app.config import DesktopConfig
from app.main_window import MainWindow
from app.tasks import TaskRunner
from app.theme import apply_theme


def main(argv: list[str] | None = None) -> int:
    """Start the client and return the process exit code."""
    application = QApplication(argv if argv is not None else sys.argv)
    application.setApplicationName("NEXUS")
    application.setApplicationDisplayName("NEXUS")
    apply_theme(application)

    config = DesktopConfig.from_environment()
    client = NexusClient(config)
    runner = TaskRunner()

    window = MainWindow(client=client, config=config, runner=runner)
    window.show()
    window.refresh_all()
    return application.exec()


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())

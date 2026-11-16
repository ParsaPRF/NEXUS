"""Entry point for the NEXUS desktop client.

    uv run python -m app.main

Environment:

* ``NEXUS_DESKTOP_API_URL`` - where the backend is (default ``http://127.0.0.1:8000``)
* ``NEXUS_DESKTOP_API_KEY`` - the ``X-API-Key`` value (falls back to ``NEXUS_API_KEY``)

Command line:

* ``--self-check [report.json]`` - build every page, probe the API, write a JSON report and exit
  with 0 (healthy) or 1. This exists so the **packaged executable** can be verified rather than
  merely produced: Qt platform-plugin failures only appear at run time, and a windowed build has
  nowhere to print a traceback.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

from app.api_client import NexusClient
from app.config import DesktopConfig
from app.main_window import MainWindow
from app.tasks import TaskRunner
from app.theme import apply_theme

SELF_CHECK_FLAG = "--self-check"


def _ensure_streams() -> None:
    """Guarantee that ``sys.stdout`` and ``sys.stderr`` exist.

    A PyInstaller **windowed** build sets ``sys.stdout`` and ``sys.stderr`` to ``None``. Anything
    that writes to them - a Qt warning, a stray ``print``, Python's own teardown - then raises
    ``AttributeError`` at an awkward moment, and errors raised during interpreter shutdown are
    reported as a non-zero exit status. A build that worked can therefore look like it failed.

    Diagnostics go to ``%LOCALAPPDATA%\\NEXUS\\desktop.log`` so a windowed build stays debuggable;
    if that cannot be opened, output is discarded rather than left as ``None``.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    sink: Any
    try:
        log_dir = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "NEXUS"
        log_dir.mkdir(parents=True, exist_ok=True)
        sink = (log_dir / "desktop.log").open("a", encoding="utf-8", buffering=1)
    except OSError:
        sink = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115

    if sys.stdout is None:
        sys.stdout = sink  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = sink  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    """Start the client and return the process exit code."""
    _ensure_streams()

    args = list(argv if argv is not None else sys.argv[1:])
    self_check = SELF_CHECK_FLAG in args
    report_path = _report_path(args)

    if self_check:
        # A built executable may run on a machine with no display (CI, a headless check); the
        # offscreen platform lets the self-check exercise Qt without one.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    application = QApplication([sys.argv[0], *args])
    application.setApplicationName("NEXUS")
    application.setApplicationDisplayName("NEXUS")
    apply_theme(application)

    config = DesktopConfig.from_environment()
    client = NexusClient(config)
    runner = TaskRunner()

    window = MainWindow(client=client, config=config, runner=runner)
    window.show()
    window.refresh_all()

    if self_check:
        return _run_self_check(application, window, config, report_path)
    return application.exec()


def _emit(payload: str) -> None:
    """Write to stdout when there is one.

    A PyInstaller windowed build sets ``sys.stdout`` to ``None``; calling ``print`` there raises
    ``AttributeError``, which would kill the self-check before it could report anything.
    """
    if sys.stdout is not None:
        print(payload)


def _report_path(args: list[str]) -> Path | None:
    """Return the report path when ``--self-check <path>`` was given."""
    if SELF_CHECK_FLAG not in args:
        return None
    index = args.index(SELF_CHECK_FLAG)
    if index + 1 < len(args) and not args[index + 1].startswith("-"):
        return Path(args[index + 1])
    return Path("nexus-selfcheck.json")


def _run_self_check(
    application: QApplication,
    window: MainWindow,
    config: DesktopConfig,
    report_path: Path | None,
) -> int:
    """Exercise the UI, probe the API, write a report, and exit with a status code.

    Two delays rather than one: the first page load has to finish before the readiness probe is
    fired, and the probe has to finish before anything is read. A single delay would make the
    check flaky on a slow machine, and a flaky check is worse than none.
    """
    from PySide6.QtCore import QTimer

    result: dict[str, Any] = {
        "ok": False,
        "qt_platform": application.platformName(),
        "base_url": config.base_url,
        "api_key_configured": bool(config.api_key),
    }

    def probe() -> None:
        window.views[5].probe()  # Settings page

    def collect() -> None:
        """Gather the result and write it, whatever happens.

        Everything is wrapped because a windowed build has **no stdout and no stderr**: an
        exception here would vanish and the process would simply exit non-zero with nothing to go
        on. That is exactly what happened the first time a packaged build was checked, so the
        report now records the failure instead of dying silently.
        """
        try:
            dashboard = window.views[0]
            status_message = window.statusBar().currentMessage()
            result.update(
                {
                    "pages": len(window.views),
                    "nav_items": window.nav.count(),
                    "dashboard_subtitle": dashboard._subtitle.text(),  # noqa: SLF001
                    "healthy_proxies": dashboard.healthy_proxies.value_text,
                    "active_inboxes": dashboard.active_inboxes.value_text,
                    "activity_entries": len(window.activity.entries),
                    "readiness_ok": "ready=" in status_message,
                    "status_message": status_message,
                }
            )
            result["ok"] = bool(
                result["pages"] == 7
                and result["nav_items"] == 7
                and result["activity_entries"] > 0
                and result["readiness_ok"]
            )
        except Exception as exc:  # noqa: BLE001 - the report is the output channel
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"

        payload = json.dumps(result, indent=2)
        if report_path is not None:
            try:
                report_path.write_text(payload, encoding="utf-8")
            except OSError as exc:
                result["report_write_failed"] = str(exc)
        _emit(payload)

        # An exception raised inside a Qt slot is invisible in a windowed build (no console), so
        # shutting down must not escape this slot. Any failure is recorded in the report instead.
        try:
            window.close()
            application.quit()
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            result["shutdown_error"] = f"{type(exc).__name__}: {exc}"
            if report_path is not None:
                with contextlib.suppress(OSError):
                    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            application.quit()

    QTimer.singleShot(1500, probe)
    QTimer.singleShot(3500, collect)
    application.exec()
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())

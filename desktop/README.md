# NEXUS desktop client (PySide6)

A dark desktop front end for the NEXUS API: dashboard, proxy manager, email manager, registration
lab, task monitor, settings and logs.

```powershell
# 1. The backend must be running (see ../README.md)
# 2. Install the client
uv sync

# 3. Point it at the backend
$env:NEXUS_DESKTOP_API_URL = "http://127.0.0.1:8000"
$env:NEXUS_DESKTOP_API_KEY = "<your-api-key>"     # falls back to NEXUS_API_KEY

# 4. Run it
uv run python -m app.main

# Tests (offscreen: no display required)
$env:QT_QPA_PLATFORM = "offscreen"
uv run pytest
uv run ruff check .
```

## The one rule

**The client is a pure API consumer.** It does not import `backend/app`, does not open the
database, and contains no business logic
([DECISION_LOG.md](../docs/DECISION_LOG.md) D7). Consequences worth knowing:

* the backend can change internally without touching this project, as long as the documented API
  holds;
* a bug here cannot corrupt data - the worst case is a wrong display;
* a future web UI is another client, not a rewrite.

## Architecture

```
app/
├── api_client.py    HTTP client: typed methods, error translation. NO Qt imports.
├── config.py        NEXUS_DESKTOP_* environment variables.
├── tasks.py         QRunnable + signals; every API call runs off the GUI thread.
├── widgets.py       DictTableModel, StatCard, toolbars. One table model for every table.
├── theme.py         Dark palette + stylesheet, role-named colour tokens.
├── activity.py      Bounded, observable client activity log.
├── main_window.py   Navigation rail, stacked pages, one status bar, monitor timer.
└── views/           One module per page; each view only talks to the client and the runner.
```

Three decisions shape everything else:

1. **All logic that can be wrong lives outside Qt.** URL building, auth, error mapping and parsing
   are in `api_client.py`, which is tested with an injected transport in milliseconds. The views
   are thin enough to be obviously correct.
2. **No blocking call on the GUI thread.** `TaskRunner` submits to a `QThreadPool` and reports
   through signals. A test asserts the work really runs on another thread.
3. **One table model.** `DictTableModel` renders lists of dictionaries, which is exactly what the
   API returns. A mapping layer per view is where columns silently drift from the API; there is
   one place to change instead.

## Testing

`tests/fake_api.py` serves the **documented response shapes** (copied from
`docs/API_REFERENCE.md`) through `httpx.MockTransport`. That means the tests fail if the client
starts expecting a field the API does not send - the drift a desktop client is most prone to.

* `test_api_client.py` - headers, params, error translation, configuration (no Qt).
* `test_widgets_and_tasks.py` - table model, cards, and off-thread execution.
* `test_views.py` - every view constructed, refreshed and inspected, plus navigation and message
  routing in the main window.
* `test_theme_and_activity.py`, `test_formatting.py` - pure logic.

Qt needs a platform plugin; the tests force `offscreen` in `tests/conftest.py` before importing
Qt, so they run on a headless machine and in CI.

## Packaging

```powershell
# from the repository root
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force   # if Windows blocks the script
& .\scripts\build-desktop.ps1                     # publishes to the first roomy non-system drive
& .\scripts\build-desktop.ps1 -OutputRoot "G:\NEXUS-build"
```

The script runs the tests first, builds with PyInstaller, publishes the application folder, and then
**verifies the published executable**: it runs `NEXUS.exe --self-check <report>` against a running
backend and fails if the app does not start, build every page and reach the API. See
[DEPLOYMENT.md](../docs/DEPLOYMENT.md) §6 for the options and the two PowerShell traps it handles.

`nexus.spec` builds **onedir** and prunes the Qt modules this application does not use
(WebEngine, Quick/QML, 3D, Charts, Multimedia). Result: ~120 MB, no Python needed on the target.

## Limitations (stated plainly)

* **No installer yet.** The build produces a folder; an MSI/Inno Setup installer is packaging
  polish rather than capability, and it was left unwritten rather than half-written. The executable
  is also unsigned, so SmartScreen warns on first run.
* **Exports are reported, not saved.** The client reports the row count from the API; a save dialog
  needs the packaging decision above (and a file dialog is painful to test headlessly).
* **The Task Monitor polls** every 2 seconds while it is the visible page. Polling a hidden page
  would be wasted work, so the timer is stopped when you navigate away.
* **HTML email bodies are never rendered** - they are shown as a notice. An email body is
  untrusted input, and rendering it is how a QA tool becomes an XSS vector.

## See also

* [API_REFERENCE.md](../docs/API_REFERENCE.md) - every endpoint this client calls
* [ARCHITECTURE.md](../docs/ARCHITECTURE.md) - where the client sits in the system
* [ROADMAP.md](../docs/ROADMAP.md) - Phase 6 record and Phase 7 plan
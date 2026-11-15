# desktop/

**Not implemented yet — this is Phase 6.**

This directory will hold the PySide6 desktop client: a dark-themed UI with Dashboard, Proxy
Manager, Email Manager, Registration Lab, Task Monitor, Settings and Logs views.

## The one rule that applies from day one

The desktop application **is a pure client of the HTTP API**. It must not import anything from
`backend/app`, must not open the database, and must not contain business logic. See
[ARCHITECTURE.md](../docs/ARCHITECTURE.md) §2 (layer view) and
[DECISION_LOG.md](../docs/DECISION_LOG.md) D7.

Two reasons that rule exists:

1. **Testability.** The backend is fully testable without a GUI, and a UI change cannot corrupt
   data.
2. **The web migration path.** Moving NEXUS to a browser UI later becomes "write another client",
   not "rewrite the application".

## Threading, before you write any code here

Qt owns its own event loop and asyncio owns a different one. **Never run an asyncio loop in the
Qt GUI thread**, and never make a blocking HTTP call on it — either freezes the interface.

The intended pattern:

- All HTTP calls go through a client service that runs off the GUI thread.
- Results return to the GUI thread via Qt signals/slots.
- Long operations are driven by the backend's background jobs; the UI polls (or is notified) and
  renders progress. The UI never performs the long operation itself.

That last point is what keeps the interface responsive while a campaign runs, which is an explicit
requirement in [ROADMAP.md](../docs/ROADMAP.md) Phase 6 exit criteria.

## See also

- [ROADMAP.md](../docs/ROADMAP.md) — Phase 6 task list
- [API_REFERENCE.md](../docs/API_REFERENCE.md) — the endpoints this client will call
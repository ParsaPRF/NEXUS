# NEXUS — Implementation Roadmap

**Rule:** one phase at a time. A phase is done only when its *exit criteria* are met with
**actually executed** tests — not when the code merely exists.

Legend: ✅ done · 🔄 in progress · ⬜ not started · 🚫 blocked by environment

---

## Phase status

| Phase | Name | Status | Deliverable |
|---|---|---|---|
| 0 | Discovery & Architecture | ✅ complete | This document set (`docs/`) |
| 1 | Project Foundation | 🔄 in progress | Runnable, tested backend skeleton |
| 2 | Proxy Manager | ⬜ | Import → normalize → validate → monitor |
| 3 | Email Manager | ⬜ | Mailpit + test inboxes + message API |
| 4 | Registration Lab | ⬜ | Demo target + adapters + verification E2E |
| 5 | Task Manager | ⬜ | Redis/RQ jobs, progress, cancellation, reports |
| 6 | Desktop UI | ⬜ | Polished dark PySide6 client |
| 7 | Finalization | ⬜ | Full suite, security review, Docker verification, docs |

---

## Phase 0 — Discovery & Architecture ✅

**Goal:** decide *what* we are building and *how*, before writing features.

| # | Task | Artifact |
|---|---|---|
| 0.1 | Inspect workspace, toolchain, services | [TECHNICAL_ASSESSMENT.md](TECHNICAL_ASSESSMENT.md) |
| 0.2 | State product scope and non-goals | [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) |
| 0.3 | Define layers, boundaries, flows | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 0.4 | Design schema, constraints, indexes | [DATABASE_DESIGN.md](DATABASE_DESIGN.md) |
| 0.5 | Record architecturally significant decisions | [DECISION_LOG.md](DECISION_LOG.md) |
| 0.6 | Map safety constraints to mechanisms | ARCHITECTURE / DECISION_LOG D8 |
| 0.7 | Sequence the work | this file |

**Exit criteria:** all seven artifacts exist and are internally consistent. ✅ Met.

**Key outcomes:** modular monolith (D1) · adapters at boundaries (D2) · zero-infrastructure
local profile (D3) · async-first (D4) · env-only secrets (D5) · SSRF guard (D6).

---

## Phase 1 — Project Foundation 🔄

**Goal:** a backend that starts, migrates, authenticates, logs safely, and has a passing test
suite — with **no features yet**. Everything after this phase is additive.

| # | Task | Files | Done when |
|---|---|---|---|
| 1.1 | Init uv project, deps, tool config | `backend/pyproject.toml`, `.python-version` | `uv sync` succeeds |
| 1.2 | Environment template | `.env.example`, `.gitignore` | No real secrets committed |
| 1.3 | Settings + validation | `app/core/config.py` | Bad config fails at startup with a clear message |
| 1.4 | Structured logging + secret redaction | `app/core/logging.py` | A logged secret appears as `***REDACTED***` |
| 1.5 | Exception hierarchy → HTTP mapping | `app/core/exceptions.py` | Every class maps to one status |
| 1.6 | SSRF / target guard | `app/core/net_guard.py` | Unit tests block loopback, private, link-local, metadata |
| 1.7 | Async DB engine + session dependency | `app/database/*` | Works on SQLite and Postgres URLs |
| 1.8 | Domain models (all 14 tables) | `app/models/*` | Imports cleanly, FKs resolve |
| 1.9 | Alembic wiring + initial revision | `alembic/`, `alembic.ini` | `alembic upgrade head` creates the schema |
| 1.10 | FastAPI app + middleware + routers | `app/main.py`, `app/api/v1/*` | `/health`, `/health/ready`, `/api/v1/...` respond |
| 1.11 | API key auth dependency + audit hook | `app/core/security.py` | 401 without key, 200 with it |
| 1.12 | Test suite (unit + integration) | `tests/` | `uv run pytest` green |
| 1.13 | Lint config | `ruff` in `pyproject.toml` | `uv run ruff check .` clean |
| 1.14 | Docker Compose (authored, unverified) | `docker-compose.yml` | Valid YAML; **not** run — daemon down |
| 1.15 | Core docs | `README.md`, `DEVELOPMENT_GUIDE.md` | A newcomer can start the API |

**Exit criteria:**
1. `uv run pytest` passes with no external services running.
2. `uv run alembic upgrade head` creates every table on SQLite.
3. `uv run uvicorn app.main:app` serves `/health` and `/health/ready`.
4. An unauthenticated request to `/api/v1/*` is rejected.
5. `uv run ruff check .` is clean.

**Not in Phase 1:** any proxy/email/registration business logic, any Qt code, any Redis code.

---

## Phase 2 — Proxy Manager ⬜

| # | Task |
|---|---|
| 2.1 | `ProxyParser`: parse `scheme://user:pass@host:port` and `host:port`; normalize scheme, host case, default ports |
| 2.2 | Importers: TXT, CSV, manual — **data-only parsing**, size-capped, no code execution |
| 2.3 | Dedupe + `ProxySource` attribution with accepted/rejected counts |
| 2.4 | Repository + `ProxyService` CRUD, filtering, sorting, pagination |
| 2.5 | Health checker: bounded-concurrency async checks, timeout, latency recording, `proxy_checks` history |
| 2.6 | Expiry detection policy (N consecutive failures → `expired`) |
| 2.7 | CSV/JSON export of selected profiles |
| 2.8 | Router + schemas; audit events for import/check/export |
| 2.9 | Tests: parser table-driven, importer edge cases, checker against a local stub server |

**Exit criteria:** import a 1,000-line list → dedupe → check against a local stub → filter by
`healthy` → export CSV, all covered by tests. **Explicitly not wired to registration.**

---

## Phase 3 — Email Manager ⬜

| # | Task |
|---|---|
| 3.1 | Compose Mailpit; SMTP + HTTP API config |
| 3.2 | `MailBackend` protocol + `MailpitBackend` + `InMemoryBackend` (tests) |
| 3.3 | `email_domains` seed: `example.test`, reserved-only guard |
| 3.4 | Inbox creation: single + bulk, random aliases, collision-safe |
| 3.5 | SMTP receipt + idempotent ingestion (`UNIQUE(inbox_id, backend_message_id)`) |
| 3.6 | Message API: list, filter, search, detail; HTML stored but never rendered |
| 3.7 | Verification-code extraction as a pure, fully-tested function |
| 3.8 | Expiration + cleanup job; JSON/CSV export |
| 3.9 | Tests: extraction corpus, idempotent ingestion, expiry |

**Exit criteria:** create inbox → send SMTP message directly to Mailpit → message visible via
API → code extracted → inbox expires and is cleaned up.

---

## Phase 4 — Registration Lab ⬜

| # | Task |
|---|---|
| 4.1 | Bundled demo target: register → send code → verify → status (own DB, own tests) |
| 4.2 | Adapter protocol: `RegistrationAdapter`, `EmailVerificationAdapter`, `AccountStatusAdapter` |
| 4.3 | `RegistrationScenario` + `RegistrationResultAnalyzer` |
| 4.4 | Allowlist enforcement on `registration_targets` (two gates: authorized + allowlisted) |
| 4.5 | CSV test-identity import + validation; synthetic credentials, never real ones |
| 4.6 | Campaign executor: conservative concurrency, fixed rate, cooperative cancel |
| 4.7 | Terminal handling of 429 / CAPTCHA / access-denied → `stopped_by_target` |
| 4.8 | Playwright adapter for the demo UI (API adapter first) |
| 4.9 | Tests: full happy path against demo target; one test per terminal outcome |

**Exit criteria:** campaign against the demo target completes end-to-end with verification,
reports per-test outcomes, and **stops** on a simulated 429/CAPTCHA.

---

## Phase 5 — Task Manager ⬜

| # | Task |
|---|---|
| 5.1 | `JobQueue` protocol + `InProcessQueue` (already used in Phases 2–4) |
| 5.2 | Redis + RQ backend and worker entry point |
| 5.3 | Job lifecycle: queued/running/completed/failed/cancelled + progress |
| 5.4 | Cooperative cancellation (`cancel_requested` checked at safe points) |
| 5.5 | Idempotency via `idempotency_key`; no duplicate execution on retry |
| 5.6 | `job_logs` streaming; duration tracking |
| 5.7 | Analytics: module stats + CSV/JSON reports |
| 5.8 | Tests: lifecycle, cancellation mid-run, retry-dedupe |

**Exit criteria:** jobs run in RQ workers, report progress, cancel safely, and a retried job
does not double-execute side effects.

---

## Phase 6 — PySide6 Desktop ⬜

| # | Task |
|---|---|
| 6.1 | Dark theme + design tokens; `QMainWindow` + sidebar navigation |
| 6.2 | API client layer (HTTPX sync or async-in-thread) with typed errors |
| 6.3 | Dashboard widgets (healthy/failed proxies, active inboxes, running campaigns, recent activity) |
| 6.4 | Proxy Manager view: table, filters, sort, details dialog |
| 6.5 | Email Manager view: inbox list, message viewer (plain text), code display |
| 6.6 | Registration Lab view: campaign wizard, progress, results table |
| 6.7 | Task Monitor: live status, logs, cancel button |
| 6.8 | Settings + Logs views; notifications/toasts |
| 6.9 | Responsiveness proof: no blocking call on the GUI thread |
| 6.10 | Tests: `pytest-qt` for view models; manual UI checklist |

**Exit criteria:** every listed view exists, the UI stays responsive during a running campaign,
and the app works against the local backend.

---

## Phase 7 — Finalization ⬜

| # | Task |
|---|---|
| 7.1 | **Start Docker Desktop** and run the full Compose stack (currently 🚫 unverified) |
| 7.2 | Real Postgres/Redis/Mailpit E2E suite (`-m e2e`) |
| 7.3 | Security review: SSRF corpus, secret redaction, auth, uploads, timeouts, dependency audit |
| 7.4 | DB integrity: constraints, cascade behaviour, migration downgrade check |
| 7.5 | Playwright E2E against the demo target |
| 7.6 | Release config, installation guide, user documentation |
| 7.7 | Cold-start check on a clean environment |

**Exit criteria:** all 12 acceptance criteria from the brief are demonstrably satisfied, with
the Docker path verified on a machine where the daemon actually runs.

---

## Critical path and sequencing notes

```
0 -> 1 -> 2 ---------\
         \-> 3 -> 4 ---> 5 -> 6 -> 7
```

- **1 → 2 and 1 → 3 can proceed in parallel**; both depend only on the foundation.
- **4 depends on 3** (verification needs inboxes) and on 2 only if proxy-backed *testing* were
  wanted — which it is not, by design. So Phase 4 does **not** depend on Phase 2.
- **5 depends on 2/3/4** because it wraps their long-running operations.
- **6 depends on 2–5** because it renders their results.
- **7 depends on everything**, plus a running Docker daemon.

## Environment blockers carried forward

| Blocker | Blocks | Clears when |
|---|---|---|
| Docker daemon not running | 7.1–7.2, 7.5 | Docker Desktop is started |
| Ports 5432/6379/8025 closed | real-infra verification in 2–5 | Compose stack is up |
| No authorized external target | a non-demo Phase 4 run | Owner supplies an authorized staging URL + note |

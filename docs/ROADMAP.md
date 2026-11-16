# NEXUS — Implementation Roadmap

**Rule:** one phase at a time. A phase is done only when its *exit criteria* are met with
**actually executed** tests — not when the code merely exists.

Legend: ✅ done · 🔄 in progress · ⬜ not started · 🚫 blocked by environment

---

## Phase status

| Phase | Name | Status | Deliverable |
|---|---|---|---|
| 0 | Discovery & Architecture | ✅ complete | This document set (`docs/`) |
| 1 | Project Foundation | ✅ complete | Runnable, tested backend skeleton |
| 2 | Proxy Manager | ✅ complete | Import → normalize → validate → monitor → export |
| 3 | Email Manager | ✅ complete | Mailpit backend, test inboxes, message API, code extraction, expiry |
| 4 | Registration Lab | ✅ complete | Demo target + adapters + verification E2E |
| 5 | Task Manager | ✅ complete | Durable jobs, monitor, cancellation, analytics, RQ backend |
| 6 | Desktop UI | ✅ complete | Dark PySide6 client, packaged and verified as a Windows executable |
| 7 | Finalization | ⚠️ complete except Docker verification | Security review, packaging, release docs; one item blocked by the build machine's Docker install |

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

## Phase 1 — Project Foundation ✅

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

## Phase 2 — Proxy Manager ✅

**Goal:** import, normalize, validate and monitor proxies the user owns — and prove each step
with tests.

**Delivered (all items from the plan):**

| # | Task | Where |
|---|---|---|
| 2.1 | Parser: URL and `host:port[:user:pass]` forms, protocol aliases, IPv6, canonical hosts | `modules/proxy_manager/parser.py` |
| 2.2 | TXT + CSV importers (header aliases or positional), size caps, no code execution | `importers.py` |
| 2.3 | De-duplication inside a file and against stored proxies; source attribution with counts | `service.py`, `repository.py` |
| 2.4 | Repository + service: filter, search, sort, paginate, enable/disable, delete | `repository.py`, `service.py` |
| 2.5 | Health checker: bounded concurrency, mandatory timeout, latency, `proxy_checks` history | `checker.py` |
| 2.6 | Expiry policy: N consecutive failures → `expired` | `service._apply_outcome` |
| 2.7 | CSV/JSON export with no credential fields | `service.export` |
| 2.8 | 9 REST endpoints + audit records for import/check/batch/export/delete | `router.py` |
| 2.9 | 157 new tests (parser, importers, checker, API) | `tests/unit/`, `tests/integration/` |
| 2.10 | Batch check as a background job with progress and cancellation points | `jobs.py` |

**Exit criteria — verified by execution:**
- import a list → dedupe → check against an injected transport → filter by `healthy` → export CSV:
  covered end to end by `tests/integration/test_proxy_api.py` (47 tests).
- parser/importers: 91 tests, table-driven over accepted and rejected forms.
- checker: 19 tests, including timeout / refusal / TLS / proxy-error classification and a
  proven concurrency cap.
- `uv run pytest` → 415 passed; `ruff check` clean.
- Credentials: refused when no key is configured, encrypted when one is, never returned by any
  endpoint or export.

**Not done on purpose:** history pruning (Phase 5 retention job) and anything connecting proxies
to registration (forbidden by design — there is no proxy parameter in the registration code).

---

## Phase 3 — Email Manager ✅

**Goal:** disposable test inboxes on a domain the owner controls, with mail ingestion and code
extraction — and no path to a third-party mailbox provider.

**Delivered:**

| # | Task | Where |
|---|---|---|
| 3.1 | Mailpit settings + Compose service (already declared in Phase 1) | `core/config.py`, `docker-compose.yml` |
| 3.2 | `MailBackend` protocol + `MailpitBackend` + `InMemoryMailBackend` | `modules/email_manager/backends.py` |
| 3.3 | `email_domains` guard: RFC-reserved or explicitly owner-verified only | `service.create_domain`, `_default_domain` |
| 3.4 | Inbox creation: single + bulk, random aliases, collision-safe, TTL | `service.create_inbox`, `bulk_create_inboxes` |
| 3.5 | Idempotent ingestion keyed on `(inbox_id, backend_message_id)` | `service._ingest` |
| 3.6 | Message API: list, search, detail; HTML stored but never rendered | `router.py` |
| 3.7 | Verification-code extraction as a pure, corpus-tested function | `extraction.py` |
| 3.8 | Expiry sweep (endpoint + job), CSV/JSON export | `service.cleanup_expired`, `export_messages` |
| 3.9 | 119 new tests | `tests/unit/`, `tests/integration/` |

**Exit criteria — verified by execution:**
- create inbox → deliver a message to the backend → poll → message visible with the code
  extracted → export → expire: covered by `tests/integration/test_email_api.py` (53 tests).
- extraction: 41 tests over labelled codes, separated digits, HTML-only bodies, and cases that
  must *not* match (years, long numbers, placeholders).
- backends: 25 tests, including Mailpit JSON parsing via an injected transport and oversized
  message rejection.
- `uv run pytest` → 534 passed; `ruff check` clean.

**Safety properties proven by tests:** every generated address is on `example.test`; a
non-reserved configured domain fails closed (403); a message addressed to another inbox is
refused; polling twice does not duplicate; expiry keeps messages rather than deleting them.

**Not done on purpose:** SMTP listening inside NEXUS (Mailpit owns it — decision D14) and history
pruning (Phase 5).

---

## Phase 4 — Registration Lab ✅

**Goal:** run registration scenarios end to end against the bundled demo target, extract the
emailed code, verify the account, and stop the moment the target raises a control.

**Delivered:**

| # | Task | Where |
|---|---|---|
| 4.1 | Bundled demo target: register → email code → verify → status, with its own store | `demo_target/app.py` |
| 4.2 | Adapter protocols + demo API adapter; outcomes instead of exceptions | `registration_lab/adapters.py` |
| 4.3 | `analyze()` + `InboxEmailVerifier` | `analyzer.py`, `verification.py` |
| 4.4 | Two-gate authorization (record + allowlist), demo targets must be local | `authorization.py` |
| 4.5 | Identity generation and CSV import, restricted to hosted domains | `identities.py` |
| 4.6 | Executor: bounded concurrency, fixed rate, cooperative cancel, serialized DB writes | `executor.py` |
| 4.7 | 429 / CAPTCHA / access-denied are terminal, campaign becomes `stopped_by_target` | `analyzer.py`, `executor.py` |
| 4.8 | **Deferred:** Playwright UI adapter (see below) | — |
| 4.9 | 80 new tests, including the full loop in-process | `tests/` |

**Exit criteria — verified by execution:**
- the whole loop runs: NEXUS API → job → adapter → demo target → its mailer → NEXUS's mail
  backend → poll → extraction → verify → `account_status == "active"`, asserted against the demo
  target's own `/api/status` (`tests/integration/test_registration_flow.py`).
- one test per terminal control: 429, CAPTCHA and access-denied each stop the campaign and are
  reported separately from product defects.
- authorization: unauthorized target → 403; authorize without a note → 422; a `kind: demo` target
  pointing at a public host → 403; an external target without allowlisting → 403.
- `uv run pytest` → 614 passed; `ruff check` clean.

**Why 4.8 was deferred (honest status):** the bundled demo target exposes a documented JSON API
and has no HTML UI, so a Playwright adapter would have nothing to drive. Shipping browser-driving
code that no test exercises would be exactly the "placeholder presented as complete" the brief
forbids. It becomes worthwhile when an authorized UI-only staging target exists; the adapter
protocol is already in place for it.

**Not done on purpose:** retrying around controls, changing identity to avoid a rate limit, and
any proxy parameter in this module. There is no proxy field on a target or a campaign.

---

## Phase 5 - Task Manager & Analytics ✅

**Goal:** make background work observable, cancellable and reportable - not merely runnable.

**Delivered:**

| # | Task | Where |
|---|---|---|
| 5.1 | Queue protocol + in-process runner (kept as the local profile) | `app/jobs/queue.py` |
| 5.2 | RQ adapter, worker entry point, sync dispatcher | `app/jobs/rq_queue.py`, `app/workers/` |
| 5.3 | Durable lifecycle: queued -> running -> terminal, with progress and duration | `task_manager/tracker.py` |
| 5.4 | Cooperative cancellation driven by the database flag | `tracker._monitor`, `service.request_cancel` |
| 5.5 | Idempotency: same key returns the same row, no second execution | `service.enqueue` |
| 5.6 | Job logs, attempts, duration; monitor API including logs | `tracker.py`, `router.py` |
| 5.7 | Analytics summary + CSV/JSON export of jobs and campaigns | `task_manager/service.py` |
| 5.8 | 40 new tests (26 task manager, 14 RQ) | `tests/integration/` |
| 5.9 | Migration 0002 adds `background_jobs.queue_job_id` | `alembic/versions/0002_job_queue_id.py` |

**Exit criteria - verified by execution:**
- lifecycle: a synthetic job is observed queued -> running -> completed with progress 3/3, logs
  and a duration, all read through the HTTP monitor (`tests/integration/test_task_manager.py`).
- idempotency: two enqueues with the same key produce exactly one row and one execution.
- cancellation: a running job stops within the monitor interval and ends `cancelled` with
  `progress_current < total`; cancelling a finished job returns 409.
- analytics: the summary counts every module; exports produce valid CSV and JSON.
- `uv run pytest` -> 654 passed; `ruff check` clean; migration 0002 applies to a fresh database.

**Honest gap:** the RQ adapter is tested against `fakeredis` (enqueue, status mapping,
cancellation) and its sync dispatcher runs a real async handler. A real RQ worker against a real
Redis remains **unverified** until Phase 7 runs Docker - there is no Redis server on this machine.

**Not done on purpose:** retry storms, job priorities and a scheduler UI (the UI is Phase 6).

---

## Phase 6 - Desktop UI (PySide6) ✅

**Goal:** a polished, dark desktop client that stays responsive while background work runs - and
that never becomes a second implementation of the business logic.

**Delivered:**

| # | Task | Where |
|---|---|---|
| 6.1 | Dark theme: palette + stylesheet with role-named colour tokens, shared status colours | `desktop/app/theme.py` |
| 6.2 | API client: typed methods, error translation, no Qt imports | `desktop/app/api_client.py` |
| 6.3 | Off-thread execution: QRunnable + signals, bounded pool | `desktop/app/tasks.py` |
| 6.4 | Reusable widgets: dict-backed table model, stat cards, toolbars | `desktop/app/widgets.py` |
| 6.5 | Seven pages: Dashboard, Proxies, Inboxes, Registration, Task Monitor, Settings, Logs | `desktop/app/views/` |
| 6.6 | Navigation rail, stacked pages, one status bar, view-visible-only monitor timer | `desktop/app/main_window.py` |
| 6.7 | Client activity log, bounded by construction | `desktop/app/activity.py` |
| 6.8 | 103 tests: client, formatting, theme, model, threading, every view, the main window | `desktop/tests/` |

**Exit criteria - verified by execution:**
- `uv run pytest` in `desktop/` -> 103 passed; `ruff check` clean.
- every view is constructed against a fake API serving the **documented response shapes**, then
  refreshed and inspected, so the tests fail if the client expects a field the API does not send.
- responsiveness: a test asserts that submitted work runs on a thread other than the GUI thread,
  and every API call goes through that path.
- keyboard/behaviour-free guarantees that matter: no API key is rendered into the status bar, HTML
  email bodies are never rendered, and the registration results pane keeps product defects and
  controls encountered separate.
- **live proof:** the client was run against a real backend (uvicorn + SQLite), and the Dashboard
  reported the seeded data (`2 proxies · 1 inboxes · 0 campaigns · 0 jobs`), the Settings page
  reported live readiness (`database: ok, mail_backend: degraded, ...`), and the proxy table showed
  the 2 imported proxies - with the credential-bearing entry correctly refused by the API.

**Known gaps:** no packaging/installer (a release build is Phase 7); the Task Monitor polls every
2 seconds while visible rather than streaming; exports are reported by row count rather than
offered as a save dialog, because file dialogs are untestable here and saving belongs to a
packaging decision.

**Not done on purpose:** no business logic in the client, no direct database access, and no
settings that could weaken the backend's safety limits from the GUI.

---

## Phase 7 - Finalization ⚠️ mostly complete

**Goal:** prove the whole thing works together, review security, and make it installable.

| # | Task | Status | Evidence |
|---|---|---|---|
| 7.1 | Start Docker and run the full Compose stack | ❌ **blocked by the environment** | engine does not run on this machine; see below |
| 7.2 | Real Postgres/Redis/Mailpit e2e suite | ✅ written, ❌ not executed | `backend/tests/e2e/test_real_infrastructure.py` (11 tests) collects and skips with a message naming what is missing |
| 7.3 | Security review | ✅ | `docs/SECURITY_REVIEW.md`: SSRF corpus (54 tests), redaction (36 tests), git-history secret scan, `pip-audit` on both projects |
| 7.4 | Database integrity and migration reversibility | ✅ | upgrade -> downgrade -> upgrade on a fresh database; `scripts/check_migration.py` reports 14 tables, 50 indexes, 13 foreign keys |
| 7.5 | Playwright UI adapter | ⏭️ deliberately deferred | the bundled demo target has no HTML UI to drive (D16) |
| 7.5b | Desktop packaging | ✅ **built and verified** | `scripts/build-desktop.ps1` + `desktop/nexus.spec`; published to `E:\NEXUS-build\dist\NEXUS` (215 files, 121.3 MB); the published `NEXUS.exe --self-check` reported `ok: true`, 7 pages, live API, **exit 0** |
| 7.6 | Release configuration and documentation | ✅ | `docs/DEPLOYMENT.md`, `docs/USER_GUIDE.md`, acceptance-criteria table in the README, compose worker command replaced with the real entry point |
| 7.7 | Cold-start check | ✅ (local profile) | fresh database migrated, server started, authenticated request served, test suite green |

### What was verified by execution in this phase

* `pip-audit` on both dependency sets: **no known vulnerabilities**.
* git history: `.env` was never committed; no credential pattern in history; none in the tree.
* migration chain: `upgrade head` -> `downgrade base` -> `upgrade head`, with the schema checked
  after each step.
* backend suite: 654 passed. Desktop suite: 103 passed. `ruff` clean in both.
* the desktop client was run against a live backend and reported real seeded data.

### The one blocked item, and exactly what was tried

Criterion 2 of the brief (Compose launches the infrastructure) could not be executed.
**Root cause established and documented** — see [DOCKER_RECOVERY.md](DOCKER_RECOVERY.md):

1. `docker info`, `docker version`, `docker ps` and `docker desktop status` all hang; local
   commands such as `docker context show` answer immediately.
2. `Get-Service com.docker*` finds **no `com.docker.service`**, and `C:\ProgramData\DockerDesktop`
   does not exist. Docker Desktop 4.87.0 is installed **per-user only**, at
   `%LOCALAPPDATA%\Programs\DockerDesktop`.
3. That missing Windows service is what launches the Linux engine, so the `docker-desktop` WSL
   distribution boots with **no `dockerd`** and no `/var/run/docker.sock`.
4. `docker desktop diagnose` agrees: `open \\.\pipe\dockerBackendApiServer: The system cannot find
   the file specified` for every backend component.
5. Virtualization is available (`HypervisorPresent = True`) and WSL2 works, so the host is not the
   problem. Docker Desktop was restarted during the review; an incomplete installation has nothing
   to start, so a restart cannot fix it.

**To close this item:** reinstall Docker Desktop **as administrator** — steps, expected output and
the checklist for recording the result are in [DOCKER_RECOVERY.md](DOCKER_RECOVERY.md) — then

```powershell
cd NEXUS
docker compose up -d
cd backend
uv run pytest -m e2e
```

The e2e module skips while services are absent, so a green default suite is not evidence that this
item passed - it is evidence that it was skipped. Recorded as F1 in `docs/SECURITY_REVIEW.md`.

### Also not done (and why)

* **An installer (MSI/Inno Setup) and code signing.** The build produces a folder you can copy or
  zip; an installer is packaging polish rather than capability, and an unsigned binary makes
  Windows SmartScreen warn on first run. Both are stated as limitations rather than half-built.
* **Proxy check-history pruning.** Deferred to the retention job noted in Phase 5.
* **Inbound API rate limiting.** The type exists, the limiter does not; recorded as F3.

---

## Definition of done for this project

The MVP is complete when a new developer can clone the repository, install, start, and drive the
whole flow (inbox -> registration -> code -> verified account -> exported report) against the
bundled demo target, with tests covering the critical path.

That is true today on the `local` profile. On the `docker` profile it is written but unproven,
for the environment reason above.

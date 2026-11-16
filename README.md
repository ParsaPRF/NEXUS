# NEXUS

**A local-first platform for authorized network resource management, disposable test inboxes, and
registration testing against applications you own.**

> ⚠️ **Scope.** NEXUS only operates against systems you own or are explicitly authorized to
> test. It does **not** create Gmail/Outlook/third-party mailboxes, does **not** store
> third-party mail passwords, and does **not** bypass CAPTCHA, rate limits or anti-bot systems.
> Those are architectural boundaries, not toggles — see [docs/SECURITY.md](docs/SECURITY.md).

---

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Discovery, architecture, database design, roadmap | ✅ complete |
| 1 | Project foundation: config, logging, security, schema, API skeleton, tests | ✅ complete |
| 2 | Proxy Manager: import, normalize, validate, monitor, export | ✅ complete |
| 3 | Email Manager: Mailpit backend, test inboxes, message API, code extraction, expiry | ✅ complete |
| 4 | Registration Lab + bundled demo target | ✅ complete |
| 5 | Task Manager: durable jobs, monitor, analytics, RQ backend | ✅ complete |
| 6 | PySide6 desktop UI: dashboard, all module views, task monitor | ✅ complete |
| 7 | Finalization, Docker verification | ⬜ not started |

**What actually works today:** the backend starts, migrates its schema, authenticates API
requests, logs with mandatory secret redaction, validates outbound URLs against SSRF, manages
proxies end to end (import, de-duplicate, filter, check, expire, export), runs disposable test
inboxes (create, poll Mailpit, extract codes, search, export, expire), and executes registration
campaigns against the bundled demo application: submit → receive the code → verify → confirm the
account is active, with per-test results and a stop when the target raises a control. Every job is
tracked durably (status, progress, logs, duration, cancellation) and reported through the job
monitor and analytics endpoints, with an RQ backend available for the docker profile. A dark
PySide6 desktop client drives all of it over the same API: dashboard, proxy manager, email
manager, registration lab, task monitor, settings and logs. Nothing is left unbuilt except the
final deployment pass (Phase 7).

---

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12+ | Developed on 3.14; the Docker image uses 3.13 |
| uv | any recent | `pip install uv` if missing |
| Git | any | |
| Docker Desktop | any | **Only needed from Phase 7** (Docker Compose, real Redis/Mailpit). Everything else runs without it. |

---

## Quick start (no Docker, no services)

```powershell
# 1. Install dependencies (creates backend/.venv)
cd NEXUS\backend
uv sync

# 2. Create your local environment file
cd ..
Copy-Item .env.example .env
#    then edit .env and set NEXUS_SECRET_KEY and NEXUS_API_KEY
#    generate values with:
#      python -c "import secrets; print(secrets.token_urlsafe(48))"

# 3. Create the database schema
cd backend
$env:NEXUS_DATABASE__URL = "sqlite+aiosqlite:///./nexus.db"
uv run alembic upgrade head

# 4. Run the API
uv run uvicorn app.main:app --reload

# 5. Check it
#    http://127.0.0.1:8000/health
#    http://127.0.0.1:8000/health/ready
#    http://127.0.0.1:8000/docs
```

On macOS/Linux replace `Copy-Item` with `cp`, and set variables with `export`.

### Making an authenticated request

Every `/api/v1/*` route requires the key from `NEXUS_API_KEY`:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/meta" `
  -Headers @{ "X-API-Key" = "<your-api-key>" }
```

```bash
curl -H "X-API-Key: <your-api-key>" http://127.0.0.1:8000/api/v1/meta
```

---

## Desktop client

With the backend running:

```powershell
cd NEXUS\desktop
uv sync

$env:NEXUS_DESKTOP_API_URL = "http://127.0.0.1:8000"
$env:NEXUS_DESKTOP_API_KEY = "<your-api-key>"      # falls back to NEXUS_API_KEY
uv run python -m app.main
```

The client is a **pure API consumer**: it never opens the database and never imports
`backend/app`. Every call runs on a worker thread and reports back through Qt signals, so the
window stays responsive while a campaign or a batch check is running.

---

## Run the tests

```powershell
cd backend
uv run pytest                 # unit + integration, no external services needed
uv run pytest -m e2e          # end-to-end, needs real Postgres/Redis/Mailpit (Phase 7)
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
```

Current baseline: **654 backend tests** and **103 desktop tests**, `ruff` clean in both projects.

```powershell
cd desktop
uv sync
$env:QT_QPA_PLATFORM = "offscreen"   # only needed on a machine without a display
uv run pytest
```

---

## Project layout

```
NEXUS/
├── backend/
│   ├── app/
│   │   ├── core/          config, logging + redaction, exceptions, security, net_guard (SSRF)
│   │   ├── database/      async engine, session dependency, declarative base
│   │   ├── models/        SQLAlchemy models for all four modules + enums
│   │   ├── schemas/       pydantic API contracts
│   │   ├── services/      use cases (currently: audit)
│   │   ├── api/           routers, middleware, health endpoints
│   │   ├── jobs/          job queue protocol + in-process implementation
│   │   └── main.py        application factory, lifespan, exception handlers
│   ├── alembic/           migration environment + 0001_initial
│   ├── scripts/           maintenance helpers
│   └── tests/             unit/ and integration/
├── desktop/               PySide6 client - imports nothing from backend/app
├── demo_target/           (Phase 4) bundled registration application
├── docker/                Dockerfile
├── docs/                  architecture, database, security, roadmap, guides
├── docker-compose.yml     Postgres + Redis + Mailpit + API + worker (unverified, daemon off)
├── .env.example           every setting, documented
└── pyproject.toml         (in backend/) dependencies, pytest, ruff
```

---

## Documentation

Start here, in this order:

| Document | Answers |
|---|---|
| [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) | What NEXUS is, and what it deliberately is not |
| [docs/TECHNICAL_ASSESSMENT.md](docs/TECHNICAL_ASSESSMENT.md) | What the environment actually provides, and the risks |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, module boundaries, runtime flows, diagrams |
| [docs/DATABASE_DESIGN.md](docs/DATABASE_DESIGN.md) | All 14 tables, constraints, indexes, and why |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, controls, and the rules that will not be relaxed |
| [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) | Phase 7 review: what was run, what it found, what remains unverified |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Local and Docker profiles, configuration, operations, troubleshooting |
| [docs/DOCKER_RECOVERY.md](docs/DOCKER_RECOVERY.md) | Why the Docker engine here does not run, and how to repair it |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Using the desktop client, and how to read a failed test |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phase-by-phase plan and exit criteria |
| [docs/DECISION_LOG.md](docs/DECISION_LOG.md) | Every significant architectural decision (ADR-lite) |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Every endpoint that exists today |
| [docs/DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md) | Set up, run, debug, troubleshoot |
| [docs/LEARNING_GUIDE.md](docs/LEARNING_GUIDE.md) | The concepts this project teaches, phase by phase |

---

## Acceptance criteria

The MVP criteria from the project brief, with the evidence for each. `⚠️` marks the one item that
could not be executed on this machine.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | The application starts reliably | ✅ | started repeatedly; `/health` and `/health/ready` answered |
| 2 | Docker Compose launches all required infrastructure | ⚠️ | file authored, validated (`docker compose config`, 5 services); **not started** — root cause found: Docker Desktop is installed per-user without `com.docker.service`, so the Linux engine cannot start. Repair + verification procedure: [docs/DOCKER_RECOVERY.md](docs/DOCKER_RECOVERY.md) |
| 3 | Proxy sources can be managed and proxies validated safely | ✅ | 47 API tests + 19 checker tests; credential refusal proven |
| 4 | Test inboxes can be created and managed | ✅ | 53 API tests; addresses only on reserved domains |
| 5 | Test emails can be received and inspected | ✅ | ingestion, search and export tested against the in-memory backend; real Mailpit covered by the e2e suite |
| 6 | The demonstration registration application works | ✅ | 22 black-box tests over its HTTP API |
| 7 | Registration tests run against the authorized demo target | ✅ | end-to-end test driving the real loop, asserting the account is active via the target's own `/api/status` |
| 8 | Email verification works end to end | ✅ | the same test: submit → code → verify → active |
| 9 | Background tasks are visible and manageable | ✅ | 26 task-manager tests: lifecycle, progress, logs, cancellation, idempotency |
| 10 | Results can be exported | ✅ | proxy CSV/JSON, message CSV/JSON, analytics jobs/campaigns CSV/JSON |
| 11 | Critical functionality has automated tests | ✅ | 654 backend + 103 desktop tests; `ruff` clean in both |
| 12 | Documentation is sufficient for another developer | ✅ | 13 documents: architecture, database, security, API, deployment, user guide, learning guide, ADRs |

Criterion 2 is tracked as an open item in [docs/ROADMAP.md](docs/ROADMAP.md) Phase 7. The tests
that would verify it are written and skip with a message naming what is missing.

---

## Safety model in one paragraph

Every outbound URL the user supplies passes one guard (`app/core/net_guard.py`) that blocks
non-HTTP schemes, loopback/private/link-local/metadata addresses, and hostnames that resolve to
them. Registration targets need **two** independent approvals: an explicit `is_authorized` flag
with a written authorization note, *and* presence in a deny-by-default host allowlist. The
registration module has no proxy parameter at all, so the proxy pool cannot be used to disguise
traffic. Test inboxes live on an RFC-reserved domain (`example.test`) backed by Mailpit, so no
third-party mailbox can be created. Anti-abuse responses from a target (429, CAPTCHA, denial)
are terminal outcomes that stop a campaign and are reported — never retried around.

---

## License

Not yet chosen. Until one is added, treat the code as all-rights-reserved.
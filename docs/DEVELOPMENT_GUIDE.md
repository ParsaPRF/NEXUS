# NEXUS — Development Guide

Everything needed to set up, run, test and debug the backend on Windows (PowerShell commands
throughout, with the bash equivalent noted where it differs).

---

## 1. Prerequisites

| Tool | Check | If missing |
|---|---|---|
| Python 3.12+ | `python --version` | install from python.org; the `py` launcher also works |
| uv | `uv --version` | `pip install uv` |
| Git | `git --version` | install Git for Windows |
| Docker Desktop | `docker info` | **Phase 7 only.** Not needed for Phases 0–6. |

---

## 2. First-time setup

```powershell
cd NEXUS

# Install the backend into backend\.venv
cd backend
uv sync

# Local environment file (never commit it)
cd ..
Copy-Item .env.example .env
```

Now edit `.env` and set at least:

```ini
NEXUS_SECRET_KEY=<random, at least 32 chars>
NEXUS_API_KEY=<random, at least 16 chars>
NEXUS_PROXY_CREDENTIAL_KEY=<another random value>
```

Generate values with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`NEXUS_PROXY_CREDENTIAL_KEY` is required only if you will store proxies that have a username and
password. Without it, NEXUS refuses such proxies rather than storing the password in plain text.

### 2.1 Initialise the database

```powershell
cd backend
uv run alembic upgrade head
```

This creates `nexus.db` (SQLite) in the current directory. Verify what was created:

```powershell
uv run python scripts/check_migration.py nexus.db
```

Expected output ends with `PASS: every documented table exists and nothing unexpected was
created`.

---

## 3. Running the application

```powershell
cd backend
uv run uvicorn app.main:app --reload
```

Then open:

| URL | What it is |
|---|---|
| http://127.0.0.1:8000/health | liveness |
| http://127.0.0.1:8000/health/ready | readiness, per dependency |
| http://127.0.0.1:8000/docs | Swagger UI (no API key needed to *view*) |

`--reload` restarts on file changes. Do not use it when benchmarking.

To authenticate:

```powershell
$headers = @{ "X-API-Key" = (Select-String -Path ..\.env -Pattern '^NEXUS_API_KEY=(.+)$').Matches.Groups[1].Value }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/meta" -Headers $headers
```

---

## 4. Tests

```powershell
cd backend
uv run pytest                        # everything that need not talk to the network
uv run pytest -v                     # verbose: one line per test
uv run pytest tests/unit             # pure logic only
uv run pytest tests/integration      # API + database
uv run pytest -k ssrf                # tests whose name matches
uv run pytest --cov=app --cov-report=term-missing   # coverage
uv run pytest -m e2e                 # real infrastructure (Phase 7, skipped by default)
```

The default suite needs **no** Docker, database server, Redis or Mailpit: it uses temporary
SQLite files and an in-process job queue. That is deliberate (Decision D9) so a test failure
always means a code problem, not a missing service.

### 4.1 What the tests cover today

| Area | File | Notable cases |
|---|---|---|
| Configuration | `unit/test_config.py` | missing/placeholder secret rejected, production safety rules |
| SSRF guard | `unit/test_net_guard.py` | loopback, private, link-local, metadata, IPv4-mapped IPv6, DNS rebinding, allowlist |
| Logging redaction | `unit/test_logging_redaction.py` | lazy `%s` args, bearer tokens, JWTs, URLs with credentials, log file content |
| Security primitives | `unit/test_security.py` | Argon2 hashing, Fernet encryption, constant-time API keys, role checks |
| HTTP API | `integration/test_api_health.py` | health, readiness, 401/403/404/405 shapes, security headers, OpenAPI |
| Database | `integration/test_models.py` | all 14 tables, FK enforcement, cascades, uniqueness, idempotency |
| Job queue | `integration/test_job_queue.py` | handler registry, redaction, cancellation fallback, concurrency cap |

---

## 5. Linting and formatting

```powershell
cd backend
uv run ruff check .          # lint
uv run ruff check . --fix    # auto-fix what is safe
uv run ruff format .         # format
uv run ruff format --check . # verify formatting without changing files
```

Ruff is configured in `pyproject.toml` with a 100-character line limit and a rule set chosen to
catch real bugs (`B` bugbear, `SIM` simplify, `ASYNC`, `RET`, `PTH`) rather than to enforce taste.

---

## 6. Migrations (Alembic)

```powershell
cd backend

uv run alembic current                  # which revision is applied
uv run alembic history                  # the chain
uv run alembic upgrade head             # apply everything
uv run alembic downgrade -1             # undo one revision
uv run alembic downgrade base           # undo everything (destructive)
uv run alembic revision --autogenerate -m "add proxy tag column"
```

**Rules**

1. Never edit a migration that has already been applied to a database you care about. Add a new
   revision instead.
2. Autogenerate is a *draft*. Review it by hand: it misses renamed indexes and some constraint
   changes.
3. Commit the generated file together with the model change.
4. On SQLite, structural changes use `batch_alter_table` (Alembic's copy-and-move strategy),
   because SQLite cannot `ALTER` most things. The env is already configured with
   `render_as_batch` for SQLite.

---

## 7. Environment variables

Full list with documentation: [`.env.example`](../.env.example). The naming rule is:

```
NEXUS_<GROUP>__<FIELD>   ->   settings.<group>.<field>
```

Examples:

```powershell
$env:NEXUS_DATABASE__URL = "sqlite+aiosqlite:///./dev.db"
$env:NEXUS_LOG_LEVEL     = "DEBUG"
$env:NEXUS_NETWORK__MAX_CONCURRENT_CHECKS = "4"
$env:NEXUS_NETWORK__ALLOWED_TARGET_HOSTS  = '["app.staging.example.test"]'   # JSON array
```

Precedence, highest first: environment variable → `.env` file → default in `config.py`.

---

## 8. Docker (Phase 7 — currently unverified)

The Docker daemon was **not running** on the development machine during Phase 0/1, so the
Compose stack has been syntax-validated but never started. Do not treat it as working until
Phase 7 runs it end to end.

```powershell
cd NEXUS
Copy-Item .env.example .env      # edit secrets first
docker compose config --quiet    # validates the file without starting anything
docker compose up -d             # requires Docker Desktop running
docker compose ps
docker compose logs -f api
docker compose down              # keeps volumes
docker compose down -v           # DELETES data
```

Services: `db` (PostgreSQL 17), `redis`, `mailpit`, `api`, `worker`. All published ports bind to
`127.0.0.1` only.

---

## 9. Common errors and what they actually mean

### `ModuleNotFoundError: No module named 'app'`

You ran `pytest` from the wrong directory, or outside `uv`. Run `uv run pytest` from
`backend/`. The package is not installed as a wheel (`tool.uv.package = false`); `pytest` finds
it through `pythonpath = ["."]` in `pyproject.toml`.

### `ValueError: NEXUS_SECRET_KEY is not set (or is still the placeholder)`

Working as designed. Create `.env` from `.env.example` and set a real key. The placeholder from
the example file is rejected on purpose.

### `sqlite3.OperationalError: index ix_... already exists` / `table ... already exists`

A model declares the same index twice — usually `index=True` on a column *and* an explicit
`Index(...)` with the same generated name in `__table_args__`. Declare it once. (This exact bug
occurred twice during Phase 1 and is now covered by the model tests.)

### `alembic.util.exc.CommandError: Can't locate revision identified by ...`

The database contains a revision id that is not in `alembic/versions/`. Either you deleted a
migration file that had been applied, or you are pointing at the wrong database. For a local
SQLite file, the quickest fix is to delete `nexus.db` and re-run `upgrade head`.

### `401 unauthenticated` on every `/api/v1` call

You did not send `X-API-Key`, or `NEXUS_API_KEY` in `.env` does not match what you sent. Note
that `.env` is read relative to the **process working directory**: start uvicorn from `backend/`
so that `../.env` resolves.

### `403 ssrf_blocked` when testing a local demo target

Private and loopback addresses are blocked by default. For local development only:

```ini
NEXUS_NETWORK__ALLOW_PRIVATE_TARGETS=true
```

This never permits cloud metadata addresses, and the production profile refuses to start with it
enabled.

### PowerShell reports `[exit code: 1]` for a command that clearly worked

PowerShell treats anything a native program writes to **stderr** as an error record. `alembic`
and `uv` write their progress to stderr, so `uv run alembic upgrade head` can report exit 1
while succeeding. Check actual output (`Running upgrade ...`) and the database, not just exit
code. Redirect with `2>&1` to merge streams.

### `docker: failed to connect to the docker API at npipe:...`

Docker Desktop is not running. Start it and wait for "Engine running". Only Phase 7 needs it.

### Tests hang for ~5 seconds on cancellation tests

Expected: `test_a_job_that_never_checks_cannot_block_shutdown_forever` deliberately tests the
forceful-cancel fallback, which waits 5 seconds before cancelling the task.

---

## 10. Debugging techniques

**See the actual SQL.** `NEXUS_DATABASE__ECHO=true` logs every statement.

**Trace one request.** Send `X-Request-ID: my-test-123`, then grep the log for it; the same id
appears in the audit trail.

**Inspect the schema that was actually built.**

```powershell
sqlite3 nexus.db ".schema proxies"
```

**Run one test with a full traceback.**

```powershell
uv run pytest tests/unit/test_net_guard.py::TestBlockedAddressRanges -vv --tb=long
```

**Check what the SSRF guard decides, without starting the app.**

```powershell
uv run python -c "from app.core.net_guard import validate_outbound_url as v; v('http://169.254.169.254/')"
```

**Ask what settings the process actually loaded.**

```powershell
uv run python -c "from app.core.config import get_settings as g; s=g(); print(s.model_dump_json(indent=2))"
```

---

## 11. Code conventions for this project

1. **Services raise domain errors, never `HTTPException`.** The API layer maps them
   (`app/main.py`, `_register_exception_handlers`).
2. **Never import another module's `models.py`.** Ask its service.
3. **`desktop/` must not import `backend/app`.** It talks HTTP.
4. **Any URL from a user goes through `net_guard` first.** No exceptions.
5. **Any new table needs a migration**, and `tests/integration/test_models.py`'s table list must
   be updated together with `docs/DATABASE_DESIGN.md`.
6. **Log through `app.core.logging.get_logger`.** The redaction filter is attached to the root
   handlers, so it applies automatically — but do not build your own handlers without it.
7. **Type hints everywhere**, and prefer explicit over clever.
8. **Docstrings explain *why*.** The code already says *what*.

---

## 12. Where to go next

Phase 2 (Proxy Manager) is the next increment. Read
[docs/ROADMAP.md](ROADMAP.md) §Phase 2 for the task list and exit criteria, and
[docs/ARCHITECTURE.md](ARCHITECTURE.md) §5.1 for the proxy-check flow you will implement.
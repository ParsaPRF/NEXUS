# NEXUS — Deployment Guide

Two supported ways to run NEXUS. Both are described here; the differences are configuration, not
code (Decision D3).

| Profile | Database | Jobs | Mail | Docker needed |
|---|---|---|---|---|
| `local` | SQLite file | in-process | in-memory | **no** |
| `docker` | PostgreSQL 17 | Redis + RQ worker | Mailpit | yes |

> **Verification status.** The `docker` profile is fully written and syntax-checked, but it has
> **not been executed on this machine**: the Docker engine here does not run (see
> `docs/SECURITY_REVIEW.md` §8). Everything in the `local` profile below has been run repeatedly.

---

## 1. Local profile (no Docker)

### 1.1 Install

```powershell
cd NEXUS\backend
uv sync                      # add --extra queue for the RQ backend, --extra postgres for PostgreSQL

cd ..
Copy-Item .env.example .env
# edit .env: set NEXUS_SECRET_KEY and NEXUS_API_KEY (generate with the command below)
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 1.2 Create the schema

```powershell
cd backend
$env:NEXUS_DATABASE__URL = "sqlite+aiosqlite:///./nexus.db"
uv run alembic upgrade head
uv run python scripts/check_migration.py nexus.db      # expects: PASS
```

### 1.3 Run

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Checks:

| URL | Expected |
|---|---|
| `http://127.0.0.1:8000/health` | `{"status":"ok", …}` |
| `http://127.0.0.1:8000/health/ready` | `ready: true` with one line per dependency |
| `http://127.0.0.1:8000/docs` | OpenAPI UI |

The client:

```powershell
cd ..\desktop
uv sync
$env:NEXUS_DESKTOP_API_URL = "http://127.0.0.1:8000"
$env:NEXUS_DESKTOP_API_KEY = "<the same key>"
uv run python -m app.main
```

### 1.4 The bundled demo target (optional, needed for registration testing)

```powershell
cd backend
uv run python -m demo_target.run      # http://127.0.0.1:9000
```

With the `local` profile the demo target's default mailer submits to SMTP on `127.0.0.1:1025`.
If Mailpit is not running, registration campaigns still work only if you point the demo target at
something that accepts SMTP; otherwise use the `docker` profile, or run Mailpit on its own:

```powershell
docker run --rm -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 axllent/mailpit
```

---

## 2. Docker profile

### 2.1 Prepare

```powershell
cd NEXUS
Copy-Item .env.example .env      # set the three secrets
docker compose config --quiet     # syntax check, no daemon needed
docker compose up -d
docker compose ps
```

Services: `db` (PostgreSQL 17), `redis`, `mailpit`, `api`, `worker`. Every published port binds
`127.0.0.1` only — the stack is not reachable from the network.

The `api` container runs `alembic upgrade head` before starting uvicorn, so the schema is applied
automatically. To do it manually:

```powershell
docker compose exec api alembic upgrade head
```

### 2.2 Verify

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready | ConvertTo-Json -Depth 4
docker compose logs -f api worker
```

Mailpit's own UI is at `http://127.0.0.1:8025` and shows exactly what arrived — useful when
debugging a verification flow.

### 2.3 Run the real-infrastructure tests

```powershell
cd backend
uv run pytest -m e2e
```

These require the stack (PostgreSQL, Redis, Mailpit) and cover the paths that the hermetic suite
cannot: dialect behaviour, real SMTP delivery, Mailpit parsing, and a real RQ worker. Override the
defaults with `NEXUS_E2E_DATABASE_URL`, `NEXUS_E2E_MAILPIT_API`, `NEXUS_E2E_REDIS_URL`.

If a service is missing the module **skips** and names what is absent; it never silently passes.

---

## 3. Configuration reference

Everything is an environment variable; the naming rule is `NEXUS_<GROUP>__<FIELD>`. The complete
annotated list is [`.env.example`](../.env.example).

Secrets — no defaults exist, so a missing value aborts startup:

| Variable | Purpose |
|---|---|
| `NEXUS_SECRET_KEY` | Signing key. ≥32 chars; the placeholder from `.env.example` is rejected |
| `NEXUS_API_KEY` | `X-API-Key` for every `/api/v1` route (≥16 chars) |
| `NEXUS_PROXY_CREDENTIAL_KEY` | Encrypts proxy credentials. Without it, credential-bearing proxies are **refused** |

Safety settings worth reviewing before a shared deployment:

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_API__HOST` | `127.0.0.1` | Bind address. Change only behind a reverse proxy you control |
| `NEXUS_NETWORK__ALLOW_PRIVATE_TARGETS` | `false` | Permits private/loopback targets. Refused outright in production |
| `NEXUS_NETWORK__ALLOWED_TARGET_HOSTS` | `[]` | Deny-by-default allowlist for external registration targets |
| `NEXUS_NETWORK__MAX_CONCURRENT_CHECKS` | `10` | Bounded fan-out |
| `NEXUS_NETWORK__HTTP_TIMEOUT_SECONDS` | `15` | Mandatory timeout on every outbound call |
| `NEXUS_PROXY__CHECK_URL` | own `/health` | Endpoint used to validate proxies — **must be one you own** |
| `NEXUS_MAIL__RESERVED_DOMAIN` | `example.test` | Reserved domain for test inboxes |
| `NEXUS_JOBS__BACKEND` | `inprocess` | `rq` uses the Redis worker |

The `production` environment additionally refuses to start if the API key is missing, private
targets are allowed, or CORS is a wildcard.

---

## 4. Operations

### 4.1 Health and readiness

`/health` is liveness (no dependencies). `/health/ready` reports each dependency separately and
reports `degraded` — not failed — for acceptable local conditions such as the in-memory mail
backend. Point orchestrator liveness at the first and readiness at the second.

### 4.2 Backups

* **SQLite:** stop the API and copy `nexus.db` — it is a single file.
* **PostgreSQL:** `docker compose exec db pg_dump -U nexus nexus > nexus.sql`
* **Mailpit:** messages are disposable by design; do not back them up.
* `.env` holds the credential key. **Losing it makes stored proxy credentials unrecoverable**,
  which is the intended behaviour (they are encrypted, not hashed).

### 4.3 Upgrades

```powershell
git pull
cd backend
uv sync
uv run alembic upgrade head
```

Migrations are the only schema authority; never edit an applied revision.

### 4.4 Resetting

* Local: delete `nexus.db` and re-run `alembic upgrade head`.
* Docker: `docker compose down -v` removes the volumes (all data).

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `uvicorn` exits with "NEXUS_SECRET_KEY is not set" | no `.env`, or only the placeholder | set a real key |
| Every `/api/v1` call returns 401 | key missing or mismatched | send `X-API-Key` matching `.env` |
| `403 ssrf_blocked` against a local demo target | private addresses blocked by default | `NEXUS_NETWORK__ALLOW_PRIVATE_TARGETS=true` **for local work only** |
| `403 target_not_authorized` for a staging app | host not allowlisted, or no authorization note | add the host to `NEXUS_NETWORK__ALLOWED_TARGET_HOSTS` and authorize the target |
| Credential-bearing proxies are refused at import | no `NEXUS_PROXY_CREDENTIAL_KEY` | set the key (existing rows are unaffected) |
| Campaign stops with `stopped_by_target` | the target answered 429 / CAPTCHA / denial | expected and correct: read `stop_reason`, fix the target's limits, re-run later |
| Desktop shows "Could not reach the NEXUS API" | backend not running, or wrong URL | check `/health`, then `NEXUS_DESKTOP_API_URL` |
| `docker: failed to connect to the docker API at npipe:…` | Docker engine not running | start Docker Desktop; the CLI accepting local commands does not mean the engine is up |
| Migration says a revision is missing | database is ahead of the code, or files were deleted | for local SQLite, delete the file and migrate again |

---

## 6. Building the desktop client as an executable

```powershell
cd NEXUS
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force   # if Windows blocks the script
& .\scripts\build-desktop.ps1
```

The script:

1. installs PyInstaller into the desktop environment (idempotent),
2. **runs the test suite first** and refuses to package a broken build,
3. builds with PyInstaller (`desktop/nexus.spec`),
4. publishes the application to another drive (`-OutputRoot`, default `E:\NEXUS-build`),
5. runs the published executable's `--self-check` against the running backend and fails the build
   if it does not start, build every page and reach the API.

| Option | Effect |
|---|---|
| `-OutputRoot <path>` | where the finished application is published (default: first non-system drive with room) |
| `-BuildRoot <path>` | where PyInstaller works (default `<repo>\build-output`) |
| `-Console` | build a console binary, for diagnosing a start-up failure |
| `-SkipSelfCheck` | build only |

Result: a self-contained folder (`NEXUS.exe` + `_internal`), about 120 MB, no Python installation
required on the target machine. Start it with `NEXUS.exe`; configure it with
`NEXUS_DESKTOP_API_URL` and `NEXUS_DESKTOP_API_KEY` as usual.

### Why two roots

On this machine the project's virtual-environment interpreter can create files **only inside the
workspace**, while other processes can write anywhere. Building straight onto another drive
therefore fails deep inside PyInstaller with `PermissionError: [WinError 5]`, which looks like a
project problem and is not. The script builds in the workspace and publishes the result, so the
bulky part stays off the system drive and the final artifact still lives where you asked.

### Two PowerShell traps this script handles for you

* **A GUI executable does not set `$LASTEXITCODE`.** Reading it after `& NEXUS.exe …` returns
  whatever a previous command left behind — which made a healthy windowed build look like a
  failure. The script uses `Start-Process -Wait -PassThru` and reads `.ExitCode`.
* **Merging a build tool's stderr can become a terminating error.** PyInstaller and uv write
  progress to stderr; with `$ErrorActionPreference='Stop'` and stream redirection that turns a
  successful command into a crash. The script captures their output to a log and checks exit codes
  explicitly.

---

## 7. What is deliberately not here

* No hosted/cloud deployment recipe. NEXUS is a local-first tool: it asserts ownership of the
  systems it touches, and exposing it publicly would make the target allowlist meaningless.
* No installer (MSI/Inno Setup). The build above produces a folder you can copy or zip; an
  installer is packaging polish, not capability, and it was not written rather than half-written.
* No code signing, so Windows SmartScreen will warn on first run of the built executable.
* No CAPTCHA/anti-bot bypass, no third-party mailbox creation, and no proxy rotation for
  registration — see `docs/SECURITY.md`.

# NEXUS — Phase 0 Technical Assessment

**Document status:** Phase 0 deliverable
**Date:** 2026-11-15
**Author role:** Software Architect / Python Engineer / DevOps

---

## 1. What this document is

Before writing a single line of application code, we inspect the machine we are going to build
on and write down what we found. This is the "discovery" part of Phase 0.

The purpose is simple: **most bad architecture decisions are made by assuming an environment
that does not exist.** We check first, then design.

---

## 2. Environment inventory (verified, not assumed)

Every value below was produced by running a command, not by guessing.

| Component | Command used | Result | Impact on NEXUS |
|---|---|---|---|
| Working directory | `Get-Location` | `C:\Users\parsa\Documents\deepseek-harness\default-workspace` | Shared scratch workspace, **not** a git repo. NEXUS gets its own subdirectory and its own git repo. |
| Workspace contents | `Get-ChildItem -Force` | ~105 loose `.sh`/`.py` scripts + 4 unrelated projects (`AnimationsInTelegram`, `arion-bot`, `arion-review`, `content-bot`) | Do **not** initialize a repo at the workspace root; do not touch unrelated files. |
| Python | `python --version` | **3.14.2** | Above the 3.12+ requirement, but very new — see Risk R1. |
| Python launcher | `py -0` | 3.14 (active), 3.13 available | We can fall back to 3.13 if a dependency lacks 3.14 wheels. |
| uv | `uv --version` | **0.13.1** | Dependency manager confirmed. No pip/venv-by-hand needed. |
| Git | `git --version` | **2.51.2.windows.1** | Available for version control. |
| Docker CLI | `docker --version` | **29.7.2** | Installed. |
| Docker Compose | `docker compose version` | **v5.4.0** | Installed (standalone `docker-compose` not required). |
| Docker daemon | `docker info` | **FAILED** — `npipe:////./pipe/dockerDesktopLinuxEngine` not found | **Docker Desktop is not running.** See Risk R2. |
| Node / pnpm | `node --version`, `pnpm --version` | v24.18.0 / 12.15.0 | Not needed for NEXUS; Playwright will use its own Python binding. |
| Port 5432 (PostgreSQL) | `Test-NetConnection` | **Closed** | No local Postgres. |
| Port 6379 (Redis) | `Test-NetConnection` | **Closed** | No local Redis. |
| Port 8025 (Mailpit) | `Test-NetConnection` | **Closed** | Mailpit not deployed yet. |
| Port 8000 (FastAPI) | `Test-NetConnection` | **Closed** | Free — safe default for the API. |

### 2.1 The one sentence summary

> The toolchain (Python, uv, git, Docker CLI, Compose) is fully installed, but **no runtime
> infrastructure is currently running** — not even the Docker daemon.

That single fact drives the most important decision in Phase 0 (Decision D3 below).

---

## 3. Risks and mitigations

### R1 — Python 3.14 is very new

**Problem.** NEXUS targets 3.12+. The installed interpreter is 3.14.2. Brand-new Python
releases routinely break C-extension dependencies (`asyncpg`, `psycopg`, `lxml`) because no
prebuilt wheels exist yet, forcing a source build that needs a C compiler on Windows.

**Why this matters.** A dependency that fails to install during Phase 1 stalls the whole
project before any feature exists.

**Mitigation (applied).**
1. Phase 1 pins the interpreter range `>=3.12` but **does not** depend on any driver that
   requires C compilation. The default database driver is `aiosqlite` (pure Python).
2. PostgreSQL runs through `asyncpg`/`psycopg` only when `DATABASE_URL` points at Postgres —
   so a driver problem can never block the first phases.
3. If a 3.14 wheel is missing, `py -3.13 -m uv ...` is the documented escape hatch.

### R2 — Docker daemon is not running

**Problem.** `docker compose up` cannot be verified on this machine right now.

**Mitigation (applied).**
1. Phase 1 must be **runnable and testable with zero external services**. This is a design
   constraint, not a workaround: it also makes the automated test suite fast and hermetic.
2. `docker-compose.yml` is authored and syntax-validated in Phase 1, but its end-to-end
   verification is explicitly deferred to Phase 7 (and flagged as *unverified* until then).
3. We never claim "Docker works" until we have actually run it. See Rule 11.9.

### R3 — Dependency version drift

A project started in late 2026 must not carry pins from a 2024 tutorial. All pins in
`pyproject.toml` were resolved by uv against the live index during Phase 1.

### R4 — Scope risk (the real project killer)

The brief describes 4 modules, ~12 database entities, a REST API, a desktop GUI, background
workers, Docker, and Playwright — realistically several weeks of work. Trying to build it in
one pass produces an unmaintainable pile.

**Mitigation.** Strict phase gates (see `ROADMAP.md`). Each phase ends with a working,
tested artifact. Phase 0 produces **documents only**. Phase 1 produces a foundation that
starts but has almost no features.

---

## 4. Architectural decisions taken in Phase 0

Decisions are recorded formally in `DECISION_LOG.md`. Summary:

| ID | Decision | One-line reason |
|---|---|---|
| D1 | Modular monolith in a single Python package | Boundaries enforced by directory + import rules, not network calls. Cheapest thing that scales to a team. |
| D2 | Adapter pattern at every external boundary (email backend, registration target, queue) | The demo target and Mailpit must be replaceable without touching business logic. |
| D3 | **SQLite + in-process queue as the zero-infrastructure default; Postgres + Redis as configuration** | Lets us build and test today with no Docker daemon (R2). |
| D4 | Async-first (FastAPI + SQLAlchemy 2.x async + HTTPX) | Proxy health checks are I/O-bound and fan out in parallel; this is exactly what asyncio is for. |
| D5 | Secrets only via environment variables, validated at startup | Prevents credentials in git history; fails fast on misconfiguration. |
| D6 | SSRF guard on all user-configurable URLs | The app fetches URLs a user supplies — that is an SSRF sink by definition. |
| D7 | Desktop UI talks to the backend **only** over the REST API | Keeps the UI replaceable (web later) and prevents business logic leaking into Qt widgets. |
| D8 | Ownership markers as a hard project rule, not a checkbox | See Section 5 — the safety constraints are architectural. |

---

## 5. The safety constraints, restated as architecture

The brief contains explicit prohibitions. These are not decoration; if they are enforced only
by good intentions they will not survive contact with a later feature request. So each one is
mapped to a mechanism that makes the unsafe thing *structurally hard*:

| Constraint from the brief | Mechanism that enforces it |
|---|---|
| No automatic Gmail/Outlook/third-party mailbox creation | Email Manager has exactly one backend adapter: `MailpitAdapter` against a **locally reserved** domain (`example.test`). There is no code path that speaks to a third-party mail provider, and no stored credentials for one. |
| No public proxies in the registration engine | `RegistrationTarget` URLs are validated against an explicit allowlist. A campaign's HTTP client can only be built from an allowlisted base URL. Proxy selection is not a parameter of registration code at all. |
| Only owned / explicitly authorized targets | `ALLOWED_TARGET_HOSTS` is checked by the SSRF/target guard before any request; the bundled demo target is the default. |
| Stop on 429 / CAPTCHA / access-denied | The registration executor treats these as **terminal, reportable outcomes**, not retryable errors. Enforced in the adapter contract and covered by tests. |
| No arbitrary code execution from imported files | Proxy lists and CSVs are parsed as **data** (`csv`/`json`/line parsing). `eval`, `exec`, `pickle`, and shell-outs on imported content do not exist in the codebase. |
| No bypassing rate limits / anti-bot | Concurrency limits and fixed rates are configuration with conservative defaults; there is no retry-with-different-identity logic. |

If a future phase repeatedly asks to weaken one of these, that is a signal to stop and
re-evaluate the project rather than to relax the guard.

---

## 6. What is buildable *today* vs. what is blocked

**Buildable today (no daemon, no services):**
- Project skeleton, configuration, logging, secret redaction
- Domain models + migrations (SQLite)
- Proxy parsing/normalization + SSRF guard + test suite
- Test-inbox abstraction with an in-memory backend
- FastAPI application + health/readiness endpoints
- Full pytest suite

**Blocked on the Docker daemon (Phase 2+ verification only):**
- Real PostgreSQL, Redis, Mailpit containers
- RQ worker process integration test
- Playwright end-to-end run against the demo target

**Nothing in the design requires Docker to be *written* — only to be *verified*.** We keep that
distinction explicit in every phase report, because "it should work" is not a test result.

---

## 7. Conclusion

The environment supports NEXUS with two adaptations: default to zero-infrastructure local
development, and keep the interpreter/driver dependency surface small until Phase 2. Neither
adaptation changes the target architecture — both are configuration, which is what we want.

**Proceed to Phase 1.**

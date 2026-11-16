# NEXUS — Security Review Record (Phase 7)

**Status:** executed against the code as of Phase 7.
**Scope:** the backend API, the desktop client, the bundled demo target, dependencies, and the
repository itself.

This document records **what was actually run** and what it produced. Items that could not be
verified on this machine are marked as such rather than quietly ticked.

---

## 1. Summary

| Area | Result |
|---|---|
| SSRF guard (threat T1) | ✅ verified by 54 unit tests covering schemes, hostnames, address ranges, DNS rebinding and allowlisting |
| Secret redaction (T3) | ✅ verified by 36 unit tests, plus integration checks: secrets do not reach logs, responses, audit rows or job payloads |
| Secrets in the repository (T4) | ✅ verified: no `.env` was ever committed; no credential pattern in git history; none in the tree |
| Credential storage (T5) | ✅ verified: passwords hashed (Argon2id), proxy credentials encrypted, refused when no key |
| Input handling (T6) | ✅ verified: imported files are parsed as inert data; no `eval`/`exec`/`pickle` anywhere |
| Denial of service (T7) | ✅ verified: size caps, mandatory timeouts, bounded concurrency, pagination everywhere |
| Authentication / authorization (T8) | ✅ verified: 401 without a key, 403 for insufficient role, constant-time comparison |
| Exposure of dev services (T9) | ✅ verified: default bind is loopback; compose publishes to `127.0.0.1` only |
| HTML injection (T10) | ✅ verified by design + test: email HTML is stored, never rendered |
| Duplicate side effects on retry (T11) | ✅ verified: idempotency key returns the existing job; message ingestion is idempotent |
| Runaway automation (T12) | ✅ verified: fixed rate, bounded concurrency, controls are terminal |
| Vulnerable dependencies (T13) | ✅ `pip-audit`: no known vulnerabilities in backend or desktop dependency sets |
| Real-infrastructure verification | ⚠️ **not executed** — the Docker engine on this machine does not run (see §8) |

---

## 2. What was run, and what it produced

### 2.1 Dependency audit (T13)

```
uv export --format requirements-txt --no-hashes --no-dev -o _req.txt
uv run --with pip-audit pip-audit -r _req.txt
```

* backend: `No known vulnerabilities found`
* desktop: `No known vulnerabilities found`

This audits the *resolved* dependency set from `uv.lock`, not just the direct dependencies, so a
transitive advisory would appear.

### 2.2 Repository secret scan (T4)

| Check | Command | Result |
|---|---|---|
| Was `.env` ever committed? | `git log --all --diff-filter=A --name-only` filtered for `.env` | never added |
| Credential patterns in history | `git log -p --all` filtered for `gho_…`, `ghp_…`, `github_pat_…`, `AKIA…`, `sk-…`, `BEGIN … PRIVATE KEY` | no matches |
| Credentials in the working tree | recursive search excluding `.git`, `.venv`, caches | no matches |
| `.gitignore` coverage | inspected | `.env`, `*.db`, `*.log`, `.venv/`, `pytest-of-*/` all ignored |

### 2.3 Test suite as evidence

| Suite | Result |
|---|---|
| `uv run pytest` (backend) | 654 passed |
| `uv run pytest` (desktop) | 103 passed |
| `uv run ruff check .` (both) | clean |
| `uv run alembic upgrade head` on a fresh database | applies `0001_initial` then `0002_job_queue_id` |
| `scripts/check_migration.py` | 14 documented tables, 50 indexes, 13 foreign keys |

### 2.4 SSRF guard (T1) — the control the brief cares most about

Verified by `tests/unit/test_net_guard.py`:

* non-HTTP schemes refused (`file`, `gopher`, `ftp`, `dict`, `jar`)
* blocked hostnames and suffixes (`localhost`, `*.local`, `*.internal`, `metadata.google.internal`)
* blocked address ranges: loopback, private v4/v6, link-local, multicast, reserved, unspecified
* **IPv4-mapped IPv6** (`::ffff:127.0.0.1`) treated as the IPv4 address it is
* **DNS rebinding**: every resolved address is judged, so a public-looking name answering with a
  private address is refused
* unresolvable hosts **fail closed**
* the development escape hatch still refuses cloud-metadata addresses
* the target allowlist is deny-by-default and is consulted **before** any DNS work

### 2.5 Redaction (T3)

Verified by `tests/unit/test_logging_redaction.py`, plus integration checks:

* `logger.info("api_key=%s", secret)` — the secret in `args` is redacted, not just the template
* bearer tokens, basic auth, JWTs, and `scheme://user:password@host` URLs
* nested dictionaries, lists and depth-limited structures
* the log **file** written on disk contains no secret (end-to-end pipeline test)
* audit rows: `detail_json` is redacted before persistence — asserted by the audit self-test
* job payloads: `background_jobs.payload_json` is redacted before storage

### 2.6 Authentication and authorization (T8)

* `/api/v1/*` without a key → `401 unauthenticated` (parametrised over every route group)
* wrong key → `401`; a *prefix* of the real key → `401` (no prefix matching)
* API key comparison uses `hmac.compare_digest`
* an empty configured key is allowed **only** outside production, logs a warning, and is reported
  as `security: degraded` by the readiness probe
* production refuses to start without a key, with the SSRF escape hatch enabled, or with wildcard
  CORS

### 2.7 Exposure (T9)

| Surface | Default | Evidence |
|---|---|---|
| API bind | `127.0.0.1` | `app/core/config.py` default; asserted by `test_bind_address_defaults_to_loopback` |
| Compose published ports | `127.0.0.1:…` for db, redis, mailpit, api | `docker-compose.yml` |
| CORS | empty list → no cross-origin headers | asserted by an integration test that sends `Origin: http://evil.test` |

### 2.8 Input handling (T6)

* proxy lists and identity CSVs are parsed with `csv` / string operations only
* a repository-wide search for `eval(`, `exec(`, `pickle`, `yaml.load`, `os.system` and
  `subprocess` in `backend/app` and `demo_target` returns nothing. The only matches in
  `desktop/app` are Qt's own `dialog.exec()` / `application.exec()`, which start an event loop -
  they do not evaluate a string, and no user input reaches them.
* imported content is size-capped **before** parsing and binary content is refused
* rejection messages quote a credential-redacted excerpt

### 2.9 Email safety (T10 and the ownership rule)

* inboxes exist only on RFC-reserved domains (`example.test`) or explicitly owner-verified ones
* a non-reserved configured domain fails closed with `403 email_domain_not_allowed`
* `body_html` is stored and returned, and the desktop client renders it as text with an explicit
  notice; there is no HTML widget for message bodies
* a message addressed to a different inbox is refused even if the backend offers it

### 2.10 Registration safety (T12 and the ownership rule)

* two gates: stored authorization **plus** an authorization note, and the host allowlist
* `kind: demo` is exempt from the allowlist **only on loopback**, so it cannot be used as a bypass
* 429 / CAPTCHA / access-denied are terminal: the campaign ends `stopped_by_target` and the outcome
  is recorded in its own bucket (`rate_limited`, `captcha_encountered`, `access_denied`)
* no retry, no identity rotation, no delay-and-resume, and **no proxy parameter exists** anywhere
  in the registration module

---

## 3. Threat-model table (restated with evidence)

See `docs/SECURITY.md` §2 for the full model; the table in §1 above maps each threat to the check
that covers it.

---

## 4. Findings and follow-ups

| # | Finding | Severity | Action |
|---|---|---|---|
| F1 | The Docker engine on this machine does not run, so real PostgreSQL/Redis/Mailpit verification did not happen. **Root cause: Docker Desktop 4.87.0 is installed per-user without `com.docker.service` or `C:\ProgramData\DockerDesktop`, so the Linux engine has nothing to start it** | verification gap, not a defect | §8; repair procedure in `DOCKER_RECOVERY.md` |
| F2 | The RQ worker path is covered by `fakeredis` and a real dispatcher run, but not by a real Redis worker | verification gap | included in the e2e suite (`tests/e2e/test_real_infrastructure.py`) |
| F3 | Rate limiting is enforced **outbound** (fixed campaign rate, bounded concurrency) but there is no inbound request-rate limit on the API | low, local-first tool | the `RateLimitedError` type exists; an inbound limiter is a candidate for a future phase |
| F4 | The desktop client does not verify a TLS certificate pin and cannot be told to trust a private CA from the GUI | low | `NEXUS_DESKTOP_VERIFY_TLS` exists; a CA-bundle option is documented as future work |
| F5 | Proxy check history is unbounded; pruning is deferred to the retention job | low | recorded in `docs/DATABASE_DESIGN.md` §3.4 and ROADMAP Phase 5 |

---

## 5. Explicitly not done (and why)

| Item | Reason |
|---|---|
| CAPTCHA solving, rate-limit evasion, identity rotation | Out of scope by design; there is no code path for them (D8) |
| Creating mailboxes on third-party providers | Out of scope by design; one Mailpit adapter, no provider code |
| Using the proxy pool for registration | Structurally impossible: no proxy parameter and no join table |
| Playwright UI adapter | The bundled demo target has no HTML UI to drive (D16) |
| Penetration testing against third-party systems | Never permitted; NEXUS only ever targets systems its owner authorizes |

---

## 6. Static review of the write paths

Every state-changing endpoint requires the admin role, writes an audit row, and is covered by a
test that asserts the row exists:

| Write path | Role | Audited | Test |
|---|---|---|---|
| `POST /api/v1/proxy-sources` | admin | `proxy.import` | `test_writes_an_audit_record` |
| `POST /api/v1/proxies/{id}/check` | admin | `proxy.check` | `test_check_writes_an_audit_record` |
| `POST /api/v1/proxies/check-batch` | admin | `job.enqueue` | `test_batch_check_runs_and_updates_proxies` |
| `DELETE/PATCH /api/v1/proxies/{id}` | admin | `proxy.delete` / `proxy.update` | API tests |
| `POST /api/v1/email-domains` | admin | `domain.create` | `test_registration_is_audited` |
| `POST /api/v1/inboxes` / `bulk` | admin | `inbox.create` / `inbox.bulk_create` | `test_creation_is_audited` |
| `POST /api/v1/targets` / `authorize` | admin | `target.create` / `target.authorize` | authorization tests |
| `POST /api/v1/campaigns` / `cancel` | admin | `campaign.create` / `campaign.cancel` | campaign lifecycle tests |
| `POST /api/v1/jobs/{id}/cancel` | admin | `job.cancel` | cancellation tests |

---

## 7. Secrets handling, end to end

1. `.env` is read by pydantic-settings, is git-ignored, and is never created by the application.
2. `NEXUS_SECRET_KEY` and `NEXUS_PROXY_CREDENTIAL_KEY` have **no defaults**; a missing value aborts
   startup with an actionable message, and the `.env.example` placeholder is rejected.
3. Local user passwords are Argon2id hashes (never reversible).
4. Proxy passwords are Fernet-encrypted; with no key configured the proxy is **refused**, not
   stored in clear text.
5. Logging, audit rows and job payloads pass through one redaction filter/serializer.
6. The API never returns a credential: `password_encrypted` has no schema field, and export code
   has no column for it.

---

## 8. Environment limitation (must not be read as a pass)

The Docker engine on the development machine is not functional, and the reason is now established
rather than suspected:

* `Get-Service com.docker*` finds **no `com.docker.service`**, and `C:\ProgramData\DockerDesktop`
  does not exist.
* Docker Desktop 4.87.0 is installed **per-user only**, at `%LOCALAPPDATA%\Programs\DockerDesktop`.
* The Linux engine is supervised by that missing Windows service, so the `docker-desktop` WSL
  distribution boots with no `dockerd` and no `/var/run/docker.sock`.
* `docker desktop diagnose` reports `open \\.\pipe\dockerBackendApiServer: The system cannot find the
  file specified` for every backend component.
* Virtualization is present (`HypervisorPresent = True`) and WSL2 works, so the host is fine.
  Restarting Docker Desktop during this review could not help: an incomplete installation has
  nothing to start.

Consequences:

* **not verified:** Compose stack startup, migrations against PostgreSQL, real Mailpit delivery,
  a real RQ worker, and the end-to-end registration flow on real infrastructure.
* **written and ready:** `tests/e2e/test_real_infrastructure.py` covers exactly those paths and
  skips with a message naming what is missing. It is lint-clean and collects correctly.
* **repair procedure:** `docs/DOCKER_RECOVERY.md` — reinstall as administrator, then
  `docker compose up -d` and `uv run pytest -m e2e` (11 tests). It also lists how to record the
  result in this document and in the roadmap.
* **tracked as:** ROADMAP Phase 7 open item.

Claiming any of those paths works would violate the project's own rule: never claim something
works without having run it.

# NEXUS — Security Model

Security is a first-class requirement of NEXUS, not a review step at the end. This document
states the threats the system is designed against, the mechanism for each, and where to find it
in the code.

---

## 1. Assets to protect

| Asset | Why it matters | Where it lives |
|---|---|---|
| API credentials | Anyone with them can drive the platform | Environment only (D5) |
| Proxy credentials | Reusable access to network paths you own | `proxies.password_encrypted`, encrypted at rest |
| Local user password | Account takeover | `users.password_hash`, one-way hash |
| Test-inbox messages | May contain verification codes for staging accounts | `messages`, local database |
| Audit trail | Evidence of what the tool did and when | `audit_logs`, append-only |
| Host network position | A laptop sits inside a trusted network | Protected by `net_guard` |

**Not an asset, because it does not exist:** third-party mailbox passwords, real user
identities, CAPTCHA-solving state. NEXUS never acquires them.

---

## 2. Threat model

| # | Threat | Mechanism in NEXUS |
|---|---|---|
| T1 | **SSRF** — a user-supplied URL makes the server fetch an internal service or cloud metadata endpoint | `app/core/net_guard.py`: scheme allowlist, host deny-list, DNS resolution + IP re-check, blocked `169.254.169.254`, size cap, mandatory timeout |
| T2 | **Target abuse** — pointing registration at a system nobody authorized | Two independent gates: `registration_targets.is_authorized` **and** `ALLOWED_TARGET_HOSTS` allowlist, both checked before any request |
| T3 | **Secret leakage via logs** | `core/logging.py` redaction filter on every log record; `payload_json`/`detail_json` redacted before persistence |
| T4 | **Secret leakage via git** | Secrets only from env; `.env` git-ignored; `.env.example` holds placeholders; no default secret values in code |
| T5 | **Credential theft from the database** | Passwords hashed (one-way); proxy credentials encrypted with an env-supplied key, and **rejected rather than stored** if no key is configured |
| T6 | **Code execution from imported files** | Proxy/CSV/JSON inputs are parsed as inert data (`csv`, `json`, line parsing). No `eval`, `exec`, `pickle`, `yaml.load`, or shell interpolation anywhere on user input |
| T7 | **Denial of service by huge input** | Upload/inline size caps, bounded concurrency, mandatory timeouts, pagination on every list endpoint |
| T8 | **Unauthorized API access** | API-key dependency on all `/api/v1/*` routes; constant-time comparison; 401 without a key, 403 for insufficient role |
| T9 | **Accidental exposure of dev services** | Default bind is `127.0.0.1`; Docker services are not published beyond localhost; production profile requires an explicit host change |
| T10 | **HTML/script injection through email content** | `body_html` is stored but never rendered by the desktop UI (plain text only); API returns JSON, not HTML |
| T11 | **Duplicate side effects on retry** | `background_jobs.idempotency_key` unique constraint + idempotent ingestion constraints on messages/tests |
| T12 | **Runaway automation against a target** | Fixed rate limit and conservative concurrency (config, not adaptive); 429/CAPTCHA/denial are **terminal** outcomes that stop the campaign |
| T13 | **Vulnerable dependencies** | `uv lock` pins the full graph; dependency audit is a Phase 7 checklist item and a CI candidate |

---

## 3. Enforcing the ownership rules

These are the constraints that keep NEXUS a legitimate QA tool. Each is implemented, not
promised:

| Rule | Implementation |
|---|---|
| No third-party mailbox creation | One mail backend adapter (Mailpit/memory) against RFC-reserved domains; `email_domains` guard rejects non-reserved domains unless explicitly owner-verified |
| No stored third-party mail passwords | No such column, no such code path |
| No proxy rotation in registration | Registration code has **no proxy parameter**; there is no join table linking proxies to campaigns |
| Only authorized targets | `is_authorized` + allowlist, both required |
| Stop on anti-abuse controls | 429 / CAPTCHA / access-denied → terminal `registration_results.outcome` values (`rate_limited`, `captcha_encountered`, `access_denied`) and campaign status `stopped_by_target` |
| No CAPTCHA solving | The functionality does not exist; there is no solver dependency and no retry-with-new-identity logic |

**Review rule:** a change that weakens any row in this table is an architecture change. It
requires a `DECISION_LOG.md` entry and an explicit re-evaluation of the project's scope — not a
quiet code edit.

---

## 4. Configuration and secret handling

- All secrets are read from environment variables (`pydantic-settings`), validated at import
  time. A missing required secret aborts startup with a message naming the variable — never a
  silent insecure default.
- `SECRET_KEY` and `PROXY_CREDENTIAL_KEY` have **no** default value. There is no "development
  default key" that could accidentally ship.
- Application logs record `request_id`, route, status and duration — never request bodies,
  never `Authorization` headers, never query strings that may carry tokens.
- Redaction covers: `password`, `passwd`, `secret`, `token`, `api_key`, `apikey`,
  `authorization`, `credential`, `cookie`, plus anything matching a Bearer/JWT shape.

## 5. Authentication and authorization

- Local user account (`users`) with a hashed password; role field `admin` | `viewer`.
- API access uses an API key (`X-API-Key`) compared in constant time. This is deliberately
  simple for a local-first tool; OAuth/OIDC is out of scope until there is a real multi-user
  deployment.
- Every state-changing endpoint writes an `audit_logs` row with action, entity, outcome and
  `request_id`.

## 6. Network safety defaults

| Setting | Default | Rationale |
|---|---|---|
| `API_HOST` | `127.0.0.1` | Not exposed to the LAN unless deliberately changed |
| `HTTP_TIMEOUT_SECONDS` | 15 | Mandatory on every outbound call |
| `MAX_CONCURRENT_CHECKS` | 10 | Bounded fan-out |
| `MAX_RESPONSE_BYTES` | 1 MiB | Caps memory from a hostile response |
| `ALLOW_PRIVATE_TARGETS` | `false` | Loopback/private IPs blocked unless explicitly opted in |
| `ALLOWED_TARGET_HOSTS` | empty (demo only) | Deny-by-default allowlist |

## 7. Data handling

- Test-inbox data is disposable by design: inboxes have `expires_at` and a cleanup job.
- Verification codes are stored for debugging a failing flow — they are for synthetic staging
  accounts, not real users.
- Backups are the user's responsibility (local-first); no telemetry, no third-party calls
  except the endpoints the user explicitly configures.
- Deleting a campaign cascades to its tests and results; deleting a target is refused while
  campaigns reference it (`ON DELETE RESTRICT`).

## 8. What NEXUS explicitly will not do

Even if asked: bypass CAPTCHA, evade rate limits or anti-bot controls, create accounts on
third-party providers, use the proxy pool to disguise registration traffic, or operate against
systems the user does not own. These are architectural, not policy-level, refusals — see
Section 3.

## 9. Phase 7 security review checklist

Executed during Phase 7; the full record, commands and raw results are in
[SECURITY_REVIEW.md](SECURITY_REVIEW.md).

- [x] `net_guard` corpus: loopback, private v4/v6, link-local, metadata IP, DNS-rebinding, non-HTTP
      schemes, oversized body — **54 tests, all passing**
- [x] Redaction: secrets in payloads/logs/audit rows never appear in output — **36 tests, plus a
      log-file assertion and the audit self-test**
- [x] Auth: missing key → 401, wrong key → 401 (including a prefix of the real key), valid → 200
- [x] Uploads: oversized file, binary content, malformed encoding, embedded credential in a
      rejected line — **all covered; rejection excerpts are redacted**
- [x] Timeouts and concurrency: mandatory timeout on every outbound call; the concurrency cap is
      asserted by a test that measures peak parallelism
- [x] Dependency audit (`pip-audit` over the resolved lockfiles, backend and desktop):
      **no known vulnerabilities**
- [x] No service binds `0.0.0.0` in the default profile — asserted by a config test; compose
      publishes every port to `127.0.0.1`
- [x] `.env` is untracked and git history contains no secret — verified with
      `git log --all --diff-filter=A` and a pattern scan of every commit
- [ ] **Real infrastructure**: PostgreSQL/Redis/Mailpit behaviour is covered by
      `backend/tests/e2e/test_real_infrastructure.py`, which **skips** on this machine because the
      Docker engine does not run (SECURITY_REVIEW.md §8). Not a pass — an open item.

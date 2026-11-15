# NEXUS — API Reference

**Base URL:** `http://127.0.0.1:8000`
**Version:** 0.1.0 (Phase 1 surface only)
**Interactive docs:** `/docs` (Swagger UI) · `/redoc` · `/openapi.json`

> This document covers **what exists today**. Endpoints planned for later phases are listed at
> the end in a separate table and are clearly marked as not implemented. A reference that
> documents imaginary endpoints is worse than no reference at all.

---

## 1. Conventions

### 1.1 Content type

Requests and responses are `application/json`. There is no form-encoded input.

### 1.2 Authentication

Every route under `/api/v1` requires an API key in a header:

```
X-API-Key: <your key>
```

* The key is configured with `NEXUS_API_KEY`.
* Comparison is constant-time (`hmac.compare_digest`), so wrong keys cannot be guessed by timing.
* Health endpoints (`/health*`) are **unauthenticated** by design: a container probe cannot
  carry a key, and they expose no data beyond a version string and dependency states.
* If `NEXUS_API_KEY` is empty (development only), the API is unauthenticated. Startup logs a
  warning and `/health/ready` reports `security: degraded`. In the `production` environment the
  application refuses to start in that state.

### 1.3 Authorization

Each authenticated caller resolves to a role:

| Role | How it is obtained | Can do |
|---|---|---|
| `admin` | presenting `NEXUS_API_KEY` | read and write |
| `viewer` | presenting a read-only key (reserved for a future `NEXUS_VIEWER_API_KEY`) | read only |

A `viewer` calling a write endpoint receives `403 forbidden`.

### 1.4 Error contract

**Every** failure returns the same shape:

```json
{
  "code": "ssrf_blocked",
  "message": "The requested URL is not permitted (private, loopback, metadata or non-HTTP).",
  "details": { "hostname": "169.254.169.254", "reason": "cloud metadata address" },
  "request_id": "4f2a9c1e8b7d0a63"
}
```

| Field | Meaning |
|---|---|
| `code` | Stable machine-readable identifier. Part of the contract; assert on this, not on prose. |
| `message` | Human-readable description. Never contains secrets. |
| `details` | Optional structured context (which field, which host, which reason). |
| `request_id` | Matches the `X-Request-ID` response header and the server log line. Quote it when reporting a problem. |

**Never** returned to a client: stack traces, SQL, connection strings, file paths.

### 1.5 Status codes used

| Status | Meaning in NEXUS |
|---|---|
| 200 | success |
| 202 | accepted for processing (job enqueued) |
| 401 | missing or invalid API key |
| 403 | authenticated but refused — policy (`ssrf_blocked`, `target_not_authorized`) or role |
| 404 | unknown route or entity |
| 405 | wrong HTTP method for a known route |
| 409 | conflict with existing state (duplicate, wrong status) |
| 422 | request payload or parameters failed validation |
| 429 | NEXUS's own inbound rate limit exceeded |
| 500 | unexpected error — a bug; the request id identifies it in the logs |
| 503 | a required dependency (database, mail backend, queue) is unavailable |

### 1.6 Request correlation

Send `X-Request-ID` to have it echoed and logged. It is accepted only if it is 64 characters or
fewer and consists of letters, digits, `-`, `_` or `.`; anything else is replaced with a
generated id. The value appears on `request.state`, in every log line, in the error body, and in
`audit_logs.request_id`.

---

## 2. Operational endpoints (unauthenticated)

### `GET /health` — liveness

Answers "is this process alive?" and touches nothing external, so a database outage can never
make an orchestrator kill a healthy process.

**Response `200`**

```json
{
  "status": "ok",
  "app": "NEXUS",
  "version": "0.1.0",
  "environment": "development",
  "profile": "local",
  "time": "2026-11-15T17:39:20.027826Z"
}
```

---

### `GET /health/ready` — readiness

Answers "can this process do useful work?" and reports each dependency **separately**, so a
failure says which one is broken.

**Response `200`** (status is 200 even when `ready` is false — the body carries the detail)

```json
{
  "ready": true,
  "checks": [
    { "name": "database",     "state": "ok",        "detail": "2.98ms" },
    { "name": "mail_backend", "state": "degraded",  "detail": "in-memory backend active..." },
    { "name": "job_backend",  "state": "ok",        "detail": "in-process runner..." },
    { "name": "security",     "state": "ok",        "detail": null }
  ]
}
```

`state` values: `ok` · `degraded` · `failed` · `skipped`.

`ready` is `false` only if some check is `failed`. A `degraded` state (for example the in-memory
mail backend, or an unauthenticated API) is reported honestly but does not take the service out
of rotation.

---

### `GET /health/db` — dialect only (hidden from the schema)

```json
{ "dialect": "sqlite", "driver": "aiosqlite", "foreign_keys_enforced": true }
```

Returns the **dialect**, never the connection URL — a URL routinely embeds a password.

---

## 3. Authenticated endpoints (`/api/v1`)

### `GET /api/v1/meta` — describe this instance

```json
{
  "app": "NEXUS",
  "version": "0.1.0",
  "environment": "development",
  "profile": "local",
  "auth_enabled": true,
  "database_dialect": "sqlite",
  "mail_backend": "memory",
  "job_backend": "inprocess"
}
```

`auth_enabled` reports *whether* a key is required. The key itself is never returned.

**Errors:** `401` without a valid key.

---

### `GET /api/v1/meta/limits` — the safety limits in force

Exposes the controls so the desktop UI can display them and an operator can confirm at a glance
that no development escape hatch is enabled.

```json
{
  "allow_private_targets": false,
  "allowed_target_hosts": [],
  "http_timeout_seconds": 15.0,
  "max_concurrent_checks": 10,
  "max_response_bytes": 1048576,
  "registration_disabled_reason": "No authorized target hosts are configured; only the bundled demonstration application may be used."
}
```

`registration_disabled_reason` is `null` when at least one target host is allowlisted.

---

### `POST /api/v1/meta/audit-selftest` — audit + redaction self-test

Writes one audit record whose payload deliberately contains a value shaped like a secret, then
reports whether redaction applied. Proves the audit path works end to end.

**Response `202`**

```json
{
  "recorded": true,
  "audit_id": 1,
  "secret_was_redacted": true,
  "request_id": "4213ef7254704d62"
}
```

Disabled in the `production` environment (`403 policy_violation`): a real audit trail should
contain only real events.

---

## 4. Planned endpoints (NOT implemented — later phases)

Listed so the design is visible; none of these respond today.

| Phase | Endpoint | Purpose |
|---|---|---|
| 2 | `POST /api/v1/proxy-sources` | import proxies from text/CSV |
| 2 | `GET /api/v1/proxies` | list with filter/sort/pagination |
| 2 | `POST /api/v1/proxies/{id}/check` | health-check one proxy |
| 2 | `POST /api/v1/proxies/check-batch` | enqueue a batch check job |
| 2 | `GET /api/v1/proxies/export` | CSV/JSON export |
| 3 | `POST /api/v1/inboxes` | create a test inbox |
| 3 | `POST /api/v1/inboxes/bulk` | create many |
| 3 | `GET /api/v1/inboxes/{id}/messages` | list messages |
| 3 | `GET /api/v1/messages/{id}` | message detail |
| 3 | `POST /api/v1/messages/{id}/extract-code` | run code extraction |
| 4 | `GET /api/v1/targets` | list authorized targets |
| 4 | `POST /api/v1/targets` | register a target (requires authorization note) |
| 4 | `POST /api/v1/campaigns` | create and enqueue a campaign |
| 4 | `GET /api/v1/campaigns/{id}` | status and progress |
| 4 | `GET /api/v1/campaigns/{id}/results` | per-test outcomes |
| 5 | `GET /api/v1/jobs` | list jobs |
| 5 | `GET /api/v1/jobs/{id}` | status, progress, logs |
| 5 | `POST /api/v1/jobs/{id}/cancel` | cooperative cancellation |
| 5 | `GET /api/v1/analytics/summary` | module statistics |
| 5 | `GET /api/v1/analytics/export` | CSV/JSON report |

---

## 5. Calling the API from Python

```python
import httpx

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-API-Key": "<your key>"}

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=10) as client:
    info = client.get("/api/v1/meta").raise_for_status().json()
    print(info["profile"], info["database_dialect"])

    limits = client.get("/api/v1/meta/limits").raise_for_status().json()
    if limits["allow_private_targets"]:
        print("WARNING: the SSRF guard is relaxed on this instance")
```

## 6. Calling it from PowerShell

```powershell
$headers = @{ "X-API-Key" = "<your key>" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/meta/limits" -Headers $headers |
  ConvertTo-Json
```

## 7. Handling errors correctly

```python
response = client.get("/api/v1/meta")
if response.status_code == 401:
    raise SystemExit("Set NEXUS_API_KEY and pass it in the X-API-Key header")
if not response.is_success:
    body = response.json()
    # Branch on `code`, never on the message text, which may be reworded.
    print(f"[{body['code']}] {body['message']} (request {body.get('request_id')})")
```
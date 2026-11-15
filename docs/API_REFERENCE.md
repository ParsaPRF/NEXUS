# NEXUS — API Reference

**Base URL:** `http://127.0.0.1:8000`
**Version:** 0.1.0 (Phases 1–2)
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

## 3.5 Proxy Manager (Phase 2)

Manage proxies you own or are authorized to use: import, normalize, check, filter, export.

**Safety notes**

- A proxy credential is **never** returned by any response or included in any export. It is
  stored encrypted, and refused at import time if no `NEXUS_PROXY_CREDENTIAL_KEY` is configured.
- Health checks target `NEXUS_PROXY__CHECK_URL`, which must be a service **you** operate. NEXUS
  does not grade proxies against third-party services.
- Batch checks are bounded by `NEXUS_PROXY__BATCH_LIMIT`; a larger request is rejected before any
  work starts.
- Nothing here is connected to registration testing. That is architectural (docs/SECURITY.md 3).

---

### `POST /api/v1/proxy-sources` — import a list

```json
{
  "content": "http://proxy.example.test:8080\nproxy2.example.test:3128\n",
  "format": "auto",
  "name": "office pool",
  "kind": "manual",
  "location": null
}
```

`format` is `auto`, `txt` or `csv`. CSV accepts a header (aliases like `ip`, `user`, `pass`,
`scheme` are recognised) or positional `host,port` / `host,port,user,pass`.

**Response `201`**

```json
{
  "source_id": 1,
  "source_name": "office pool",
  "format_detected": "txt",
  "entries_seen": 2,
  "accepted": 2,
  "duplicates": 0,
  "rejected": 0,
  "rejected_samples": [],
  "credentials_refused": 0
}
```

Rejected entries carry `line_number`, a specific `reason`, and an `excerpt` with any credential
already replaced by `***REDACTED***`. Plaintext passwords are generated only by the target
application, so they can never be produced by manual input; generated passwords are only ever
stored hashed on the demo side.

**Errors:** `401` · `422 import_file_error` (oversized, binary, unknown format) · `422` body
validation.

---

### `GET /api/v1/proxies` — list with filters

Query parameters (all optional): `status` (repeatable), `protocol` (repeatable), `source_id`,
`search`, `enabled_only`, `min_latency_ms`, `max_latency_ms`, `sort`, `limit` (1–500, default 50),
`offset`.

`sort` accepts `id`, `host`, `port`, `protocol`, `status`, `latency_ms`, `last_checked_at`,
`created_at`, `success_count`, `failure_count`; prefix `-` for descending. An unknown sort field
falls back to newest-first rather than erroring.

**Response `200`**

```json
{
  "items": [
    {
      "id": 1, "source_id": 1, "protocol": "http", "host": "proxy.example.test", "port": 8080,
      "username": null, "status": "healthy", "latency_ms": 37, "reliability": 1.0,
      "success_count": 1, "failure_count": 0, "consecutive_failures": 0,
      "last_checked_at": "2026-11-15T18:20:11Z", "last_success_at": "2026-11-15T18:20:11Z",
      "last_error": null, "is_enabled": true, "created_at": "2026-11-15T18:19:02Z"
    }
  ],
  "meta": { "total": 1, "limit": 50, "offset": 0 }
}
```

---

### `GET /api/v1/proxies/stats` — dashboard counts

```json
{
  "total": 2,
  "by_status": { "healthy": 1, "unknown": 1 },
  "healthy_with_latency": 1,
  "average_latency_ms": 37.0
}
```

---

### `GET /api/v1/proxies/export` — export CSV or JSON

`format=csv|json` (default `csv`). Supports `status`, `protocol`, `search`, `enabled_only`,
`limit` (1–10000). Returns a download with `Content-Disposition: attachment`.

Exported columns: `protocol, host, port, username, status, latency_ms, reliability,
success_count, failure_count, last_checked_at, is_enabled, source_id`. There is no credential
column, by design — an export is a file that gets copied around.

---

### `GET /api/v1/proxies/{proxy_id}` — one proxy

`200` with a `ProxyRead`, or `404 not_found`.

---

### `PATCH /api/v1/proxies/{proxy_id}` — enable or disable

```json
{ "is_enabled": false }
```

Keeps the row and its check history; `enabled_only=true` filtering excludes it from checks.

---

### `DELETE /api/v1/proxies/{proxy_id}` — delete

`204`. Deletes the proxy and its check history (cascade).

---

### `POST /api/v1/proxies/{proxy_id}/check` — check one proxy

No request body.

**Response `200`**

```json
{
  "proxy_id": 1, "ok": true, "latency_ms": 37, "http_status": 200,
  "error_class": null, "error_message": null, "status": "healthy"
}
```

`ok: true` means the proxy carried the request — an endpoint answering `503` through a working
proxy is still a working proxy. `error_class` is one of `timeout`, `connect_error`, `proxy_error`,
`tls_error`, `http_error`, `unknown`.

**Expiry policy:** after `NEXUS_PROXY__EXPIRE_AFTER_FAILURES` consecutive failures the proxy is
marked `expired` (not merely `unhealthy`), because a revoked credential will not recover by itself.

---

### `POST /api/v1/proxies/check-batch` — queue a batch check

```json
{ "proxy_ids": [1, 2, 3] }
```

or select by status:

```json
{ "status_filter": "unknown", "limit": 100 }
```

**Response `202`**

```json
{ "job_id": "9f2c…", "job_type": "proxy.check_batch", "proxy_count": 3 }
```

The job checks in chunks, reports progress and honours cancellation between chunks. With the
`inprocess` backend it runs inside the API process; with `rq` (Phase 5) it runs in a worker.

**Errors:** `404` if none of the ids exist · `422` for an empty selection or more than
`NEXUS_PROXY__BATCH_LIMIT` items.

---

## 4. Planned endpoints (NOT implemented — later phases)

Listed so the design is visible; none of these respond today.

| Phase | Endpoint | Purpose |
|---|---|---|
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
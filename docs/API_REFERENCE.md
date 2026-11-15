# NEXUS — API Reference

**Base URL:** `http://127.0.0.1:8000`
**Version:** 0.1.0 (Phases 1–4)
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

## 3.6 Email Manager (Phase 3)

Disposable test inboxes on a domain the owner controls, backed by Mailpit (or an in-memory
backend in tests).

**Safety notes**

- Inboxes are only created on **RFC-reserved domains** (`example.test` and friends) or on a
  domain the operator has explicitly marked `owner_verified`. A non-reserved domain fails closed
  with `403 email_domain_not_allowed`.
- NEXUS never creates a mailbox on Gmail, Outlook or any other third-party provider. There is one
  backend adapter for the local mail infrastructure and no code path to a third party.
- `body_html` is **untrusted input**. It is stored for extraction and debugging; a client must
  never render it as markup.
- Ingestion is idempotent: polling twice returns `ingested: 0`.
- Expiry is a status change, not a deletion: messages stay until the inbox is deleted.

---

### `GET /api/v1/email-domains` — list domains

```json
[
  {
    "id": 1, "domain": "example.test", "is_reserved": true, "owner_verified": false,
    "is_active": true, "description": "Default RFC-reserved test domain", "inbox_count": 3
  }
]
```

The default domain is created on demand the first time an inbox is made.

---

### `POST /api/v1/email-domains` — register a domain

```json
{ "domain": "staging.example.org", "owner_verified": true, "description": "QA staging" }
```

`201` on success · `403 email_domain_not_allowed` when the domain is not RFC-reserved and
`owner_verified` is not `true` · `409` when already registered · `422` for a malformed domain.

---

### `GET /api/v1/inboxes` — list inboxes

Query: `status` (repeatable), `campaign_id`, `search`, `expiring_only`, `limit` (1–500),
`offset`. Returns `{ items, meta }` with the standard pagination envelope.

---

### `POST /api/v1/inboxes` — create one inbox

```json
{ "label": "manual-qa", "ttl_hours": 6, "local_part": "optional-fixed-alias" }
```

**Response `201`**

```json
{
  "public_id": "0e5b…", "address": "qa-3f9a2b7c1d4e@example.test",
  "local_part": "qa-3f9a2b7c1d4e", "domain": "example.test", "label": "manual-qa",
  "status": "active", "message_count": 0, "last_message_at": null,
  "expires_at": "2026-11-16T04:39:00Z", "campaign_id": null,
  "created_at": "2026-11-15T22:39:00Z"
}
```

`409` if a requested alias is taken · `422` for an invalid alias · `403` if the configured domain
is not usable.

---

### `POST /api/v1/inboxes/bulk` — create several inboxes

```json
{ "count": 25, "label_prefix": "campaign-7", "ttl_hours": 24 }
```

**Response `201`**: `{ "created": 25, "inboxes": [ … ] }`

Capped by `NEXUS_MAIL__MAX_BULK_INBOXES` (default 100); a larger request is `422`. The cap is a
safety limit, not a performance knob: bulk creation is for test setup, not for generating
mailboxes at scale.

---

### `POST /api/v1/inboxes/cleanup` — expire overdue inboxes

**Response `200`**: `{ "expired": 2 }`

The same sweep runs as the `inbox.cleanup` background job; this endpoint exists so the behaviour
can be triggered and observed directly.

---

### `GET /api/v1/inboxes/{public_id}` — one inbox

`200` with an inbox, or `404 not_found`.

---

### `DELETE /api/v1/inboxes/{public_id}` — delete an inbox

`204`. Deletes the inbox and every message it received (cascade).

---

### `POST /api/v1/inboxes/{public_id}/poll` — ingest new messages

No request body. NEXUS asks the configured backend for messages addressed to this inbox and
stores the new ones.

**Response `200`**

```json
{ "inbox": "qa-3f9a…@example.test", "ingested": 1, "skipped": 0, "already_known": 0 }
```

`skipped` counts messages ignored because they were addressed to another inbox, oversized, or no
longer present in the backend.

---

### `GET /api/v1/inboxes/{public_id}/messages` — list messages

Query: `search` (subject, sender, body), `limit` (1–200), `offset`. Bodies are omitted from list
items; `extracted_code` and `extraction_confidence` are included.

---

### `GET /api/v1/inboxes/{public_id}/export` — export messages

`format=csv|json` (default `csv`), `limit` (1–5000). Returns a download.

---

### `GET /api/v1/messages/{message_id}` — one message

Adds `body_text`, `body_html` and selected `headers` to the list shape.

`body_html` is untrusted; render as plain text only.

---

### `POST /api/v1/messages/{message_id}/extract-code` — re-run extraction

```json
{
  "message_id": 1, "code": "481920", "confidence": 0.9, "strategy": "labelled:text",
  "candidates": [
    { "code": "481920", "confidence": 0.9, "strategy": "labelled:text" },
    { "code": "481920", "confidence": 0.45, "strategy": "standalone:text" }
  ]
}
```

Useful after extraction rules improve, and for diagnosing a message whose code was missed.

**Extraction confidence bands:** labelled `0.9` · digit-separated `0.8` · digits near a label
`0.65` · standalone six digits `0.45`. A caller that needs certainty should require `>= 0.7`.

---

## 3.7 Registration Lab (Phase 4)

Run registration campaigns against the bundled demonstration application (or an authorized,
allowlisted target).

**Safety notes**

- A campaign requires a target that is **active**, **explicitly authorized**, carries an
  **authorization note**, and passes the host allowlist (except the local demo target).
- A target answering 429, a CAPTCHA challenge, or an access denial **stops the campaign**
  (`status: stopped_by_target`). It is never retried around, and there is no identity rotation,
  delay-and-resume or proxy option anywhere in this module.
- Identities are synthetic. Imported addresses must be on a domain this instance hosts, otherwise
  their verification email could not be read.
- Rate and concurrency are fixed at creation time and never adapt to the target.

---

### `GET /api/v1/targets` — list targets

```json
[
  {
    "id": 1, "name": "Bundled demo application", "base_url": "http://127.0.0.1:9000",
    "kind": "demo", "adapter_name": "demo_api", "is_authorized": true,
    "authorization_note": "Bundled demonstration application owned by this repository.",
    "is_active": true, "may_be_tested": true, "created_at": "2026-11-15T23:00:00Z"
  }
]
```

`may_be_tested` is a UI convenience; the executor re-checks the real gates before running.

---

### `POST /api/v1/targets` — register a target

```json
{
  "name": "Bundled demo application",
  "base_url": "http://127.0.0.1:9000",
  "kind": "demo",
  "adapter_name": "demo_api",
  "is_authorized": true,
  "authorization_note": "Bundled demonstration application owned by this repository."
}
```

The URL is validated at creation time.

| Case | Result |
|---|---|
| `kind: demo`, loopback host | `201` — allowed without an allowlist entry |
| `kind: demo`, public host | `403 target_not_authorized` — the exemption is not a bypass |
| external `kind: api`/`ui`, host not allowlisted | `403 target_not_authorized` |
| `is_authorized: true` with no note | `422` — the note is mandatory |
| same `base_url` again | `409` |

---

### `POST /api/v1/targets/{id}/authorize?note=...` — authorize a target

Records that testing this target is authorized, and by whom. Required before a campaign can run.

`200` with the updated target · `422` when the note is empty · `404` for an unknown id.

---

### `GET /api/v1/campaigns` — list campaigns

Query: `status` (repeatable), `limit`, `offset`.

---

### `POST /api/v1/campaigns` — create and queue a campaign

```json
{
  "name": "post-deploy check",
  "target_id": 1,
  "count": 3,
  "concurrency": 1,
  "rate_limit_per_minute": 20,
  "email_timeout_seconds": 30
}
```

Or import identities instead of generating them:

```json
{ "name": "imported run", "target_id": 1, "identities_text": "email\nqa-1@example.test\n" }
```

**Response `201`**

```json
{
  "campaign": {
    "public_id": "6f1c…", "name": "post-deploy check", "target_id": 1, "status": "queued",
    "total_tests": 3, "completed_tests": 0, "succeeded_tests": 0, "failed_tests": 0,
    "progress_percent": 0.0, "concurrency": 1, "rate_limit_per_minute": 20,
    "started_at": null, "finished_at": null, "stop_reason": null,
    "created_at": "2026-11-15T23:00:00Z"
  },
  "job_id": "9f2c…"
}
```

**Errors:** `403 target_not_authorized` · `404` unknown target · `422` for no identities, an
identity on an unhosted domain, or too many identities.

---

### `GET /api/v1/campaigns/{public_id}` — campaign status

Returns the campaign with progress. `status` is one of `queued`, `running`, `completed`,
`failed`, `cancelled`, `stopped_by_target`.

---

### `GET /api/v1/campaigns/{public_id}/results` — per-test results

```json
{
  "campaign": { "…": "…" },
  "items": [
    {
      "test_id": 1, "email": "qa-3f9a…@example.test", "status": "active",
      "outcome": "success", "verification_code": "481920",
      "code_extraction_confidence": 0.9, "account_status": "active",
      "duration_ms": 412, "error_message": null,
      "steps": { "submit": "accepted", "verify": "accepted", "account_status": "active" }
    }
  ],
  "meta": { "total": 1, "limit": 200, "offset": 0 },
  "summary": {
    "success": 1, "product_defects": 0, "controls_encountered": 0
  }
}
```

`summary` keeps defects and controls apart on purpose: a failed verification is a bug report, a
rate limit is a boundary that was respected.

Outcome values: `success`, `verification_failed`, `email_timeout`, `validation_error`,
`unexpected_error`, `rate_limited`, `captcha_encountered`, `access_denied`.

---

### `POST /api/v1/campaigns/{public_id}/cancel` — cancel

The runner stops before starting the next identity; a test already in flight finishes and is
recorded. `200` with the updated campaign · `409` when the campaign already reached a terminal
state.

---

### Demo target (development only)

The bundled application runs separately:

```powershell
cd NEXUS\backend
uv run python -m demo_target.run      # http://127.0.0.1:9000
```

It exposes `POST /api/register`, `POST /api/verify`, `GET /api/status/{email}` and, for testing
only, `POST /api/_simulate` with `{"mode": "rate_limit" | "captcha" | "access_denied" | "slow"}`.
That switch is what makes "stop on a control" a testable requirement rather than a claim.

---

## 4. Planned endpoints (NOT implemented — later phases)

Listed so the design is visible; none of these respond today.

| Phase | Endpoint | Purpose |
|---|---|---|
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
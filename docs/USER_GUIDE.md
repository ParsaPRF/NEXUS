# NEXUS — User Guide

How to use NEXUS to test a registration flow. Written for the person clicking the buttons, not
for the person reading the code; [`README.md`](../README.md) is the entry point for setting it up.

---

## 1. What NEXUS is for

You own a web application with a sign-up flow: a user submits an email, receives a code, enters
the code, and the account becomes active. NEXUS answers one question:

> **Does that flow still work, end to end, right now?**

It does it by creating disposable inboxes, driving the sign-up flow against a target you own, and
reporting exactly what happened for each attempt.

## 2. What NEXUS will not do

This is worth reading once, because the tool will refuse and it is not a bug:

| You ask for | What happens |
|---|---|
| Registering on a site you do not own | refused — the host must be allowlisted **and** the target must be authorized with a written note |
| Creating Gmail/Outlook accounts | impossible — inboxes only exist on a reserved local domain |
| Getting past a CAPTCHA, a 429, or an access denial | the run **stops** and records it; nothing retries around it |
| Spreading registrations across your proxy pool | impossible — registration has no proxy setting at all |
| Checking proxies against some public website | the check endpoint is configuration, and it must be one you operate |

---

## 3. Before you start

1. The backend is running (`uvicorn app.main:app`) — see [DEPLOYMENT.md](DEPLOYMENT.md).
2. The desktop client is pointed at it and you have entered the API key.
3. For registration testing: the bundled demo target is running (`uv run python -m demo_target.run`),
   or you have an authorized staging target.

The status bar at the bottom tells you what just happened. Anything unusual also appears under
**Logs**, which is the first place to look when something seems odd.

---

## 4. A first run in five minutes

1. **Proxy Manager → Import…** paste:

   ```
   proxy1.example.test:8080
   proxy2.example.test:3128
   ```

   The reply says how many were accepted, how many were duplicates and how many were rejected.
   If an entry had a password and the backend has no credential key, it is **refused** rather than
   stored in clear text — the reply tells you how many.

2. **Proxy Manager → Check unknown.** A job is queued; watch it under **Task Monitor**. Latency and
   status appear in the table. A proxy that fails repeatedly ends up `expired`, which means "this
   will not recover by itself", as opposed to `unhealthy`.

3. **Email Manager → New inbox.** You get an address like `qa-3f9a2b7c1d4e@example.test`.
   `example.test` is reserved by the internet standards precisely for this: mail sent to it cannot
   reach anybody.

4. **Registration Lab → Add bundled demo target**, then select it and **New campaign…**. Three
   identities, concurrency 1, a modest rate. The campaign is queued and runs in the background.

5. **Task Monitor** shows the job's progress and its log. **Registration Lab → results** shows one
   row per identity: `success` with the verification code, or a specific failure.

---

## 5. The pages

### Dashboard
Counts across the instance — healthy and failing proxies, active inboxes, running campaigns,
running and failed jobs — plus the most recent audit activity. It answers "is anything wrong?"
before you go looking.

### Proxy Manager
Your proxies, with filters by status and a search over host and account.

| Action | Notes |
|---|---|
| **Import…** | paste a list (one per line) or CSV. A header such as `host,port,username,password` is understood, so is `ip,port,user,pass` |
| **Check selected** | one proxy → checked immediately; several → a batch job |
| **Check unknown** | queues a batch for everything never checked |
| **Enable/disable** | keeps the row and its history but takes it out of checks |
| **Delete** | removes the proxy *and* its check history |
| **Export CSV** | applies the current filter; **never contains passwords** |

Latency and reliability come from recorded checks, so they are measurements rather than
estimates.

### Email Manager
Disposable inboxes and the mail that arrives in them.

| Action | Notes |
|---|---|
| **New inbox** | random alias, optional label, TTL (default 24 h) |
| **Create 10…** | bulk creation, capped by the backend (`NEXUS_MAIL__MAX_BULK_INBOXES`) |
| **Poll selected** | fetches new mail for that inbox. Polling twice is safe: already-stored messages are not duplicated |
| **Expire overdue** | marks inboxes past their TTL as `expired` — messages are kept until you delete the inbox |
| **Export messages** | CSV of that inbox's messages |

Selecting an inbox lists its messages; selecting a message shows the extracted verification code
and the body. **The HTML body is never rendered** — it is untrusted input, and the pane says so.

### Registration Lab
Three panels, top to bottom: targets, campaigns, results.

* **Add bundled demo target** registers the local demo application. It is the only target allowed
  without an allowlist entry, because it must be local.
* **Authorize selected…** records *who* authorized testing a target. A campaign cannot be created
  until that exists — the flag defaults to off so a forgotten step fails closed.
* **New campaign…** sets the name, how many identities, concurrency, rate and the email timeout.
  Identities are generated inboxes; you can also supply addresses by API/CSV, but they must be on a
  domain this instance hosts, otherwise their mail could not be read.
* **Results** separates `success`, `product defects` (the application misbehaved — a bug report)
  and `controls encountered` (the application refused; the run stopped). Keeping those apart is the
  point: averaging them would hide which one you are looking at.

### Task Monitor
Every background job with status, progress, attempts, duration and its log. **Cancel job** asks the
job to stop; it finishes the item it is on and stops before starting the next one, so nothing is
left half-written. The page refreshes itself every two seconds while it is visible.

### Settings
Read-only: what the backend is, the safety limits currently in force, and where this client points.
Deliberately read-only — a screen that could weaken the backend's safety limits from a GUI would be
a liability, not a feature.

### Logs
What this client did and what came back, plus a readiness probe that lists every dependency. The
backend keeps its own log with request ids; an error message here shows the request id to quote.

---

## 6. Reading a failed test

The results table gives an `outcome`. What to do about each:

| Outcome | Meaning | What to do |
|---|---|---|
| `success` | account created, code accepted, account active | nothing |
| `email_timeout` | registration accepted but no code arrived in time | check the target actually sent mail (Mailpit UI), then raise the email timeout |
| `verification_failed` | the code was rejected, or the account never became active | a defect in the application under test — this is the bug report |
| `validation_error` | the target rejected the payload, or the address already exists | check the target's own validation rules and whether the identity is new |
| `unexpected_error` | transport or protocol problem | check the target is reachable and its API unchanged |
| `rate_limited` | the target answered 429 | **the run stopped.** Wait, or ask the target's owner to raise limits — do not retry harder |
| `captcha_encountered` | the target demanded a CAPTCHA | **the run stopped.** Use a target that permits automated testing |
| `access_denied` | the target refused this client (401/403) | **the run stopped.** Fix credentials or permissions on the target |

The last three are controls, not failures. NEXUS reports them and stops by design.

---

## 7. Where your data lives

| Data | Where | Survives a restart? |
|---|---|---|
| Proxies, inboxes, messages, campaigns, results, jobs, audit trail | the backend database (`nexus.db` locally) | yes |
| Mailpit's copy of messages | Mailpit's storage | yes, until cleared |
| Client activity log | memory in the desktop process | no — by design, it is a session log |

Deleting an inbox deletes its messages (cascade). Deleting a proxy deletes its check history.
Expiring an inbox does **not** delete anything.

---

## 8. Common questions

**Can I use my own domain for inboxes?**
Yes, if you control it: register it with `owner_verified=true`. Without that confirmation NEXUS
refuses, so it can never host test addresses on a domain you do not own.

**Why was my proxy refused instead of imported?**
It had a password and the backend has no `NEXUS_PROXY_CREDENTIAL_KEY`. Import it without
credentials, or configure the key.

**Why is `NEXUS_NETWORK__ALLOW_PRIVATE_TARGETS` off?**
Because it permits reaching private addresses. It exists for local development; the production
profile refuses to start with it enabled.

**The campaign stopped even though most tests had not run.**
That is correct behaviour for a control: continuing would mean working around a limit the target
set. Fix the cause and re-run.

**Can I run NEXUS against a site to see whether its sign-up works?**
No. Only systems you own or are explicitly authorized to test, and you record that authorization
on the target before any campaign can run.

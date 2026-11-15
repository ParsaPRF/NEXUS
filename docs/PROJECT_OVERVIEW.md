# NEXUS — Project Overview

> A local-first platform for managing authorized network resources, disposable testing
> inboxes, and automated registration testing in owned or explicitly authorized applications.

This document is the "what and why". For "how it is structured" read
[ARCHITECTURE.md](ARCHITECTURE.md); for the plan read [ROADMAP.md](ROADMAP.md).

---

## 1. What problem NEXUS solves

Imagine you are building a web application that has a sign-up flow:

1. User submits email + password.
2. App sends a verification email with a 6-digit code.
3. User enters the code.
4. Account becomes active.

Now you change something in that flow. **How do you know it still works?** You cannot test it
by hand 200 times, and you cannot use your real email address for 200 accounts.

NEXUS is the tool that answers that question. It gives you:

- **Test inboxes** that exist only on your machine and that nobody can spam.
- **A registration runner** that exercises the sign-up flow against a target you own.
- **Structured reports** of what passed, what failed, and why.
- **A proxy manager** for the network paths your own infrastructure uses.

## 2. What NEXUS explicitly is not

This is the most important section of the document. NEXUS is a **quality-assurance tool for
people who own the system under test**. The following are out of scope by design, not by
omission:

| Not supported | Why |
|---|---|
| Creating Gmail / Outlook / any third-party mailbox accounts | Violates those providers' terms and is the infrastructure of spam and fraud. NEXUS only creates inboxes on a domain *you* control, backed by Mailpit. |
| Storing third-party email passwords | We never have them, so we cannot leak them. |
| Bypassing CAPTCHA, rate limits, anti-bot or account limits | A QA tool must reproduce the *real* user experience. Defeating a control means you are no longer testing the product. |
| Rotating proxies during registration runs | Would let the runner evade the target's abuse detection. Registration and proxy modules are architecturally disconnected. |
| Attacking, scanning, or registering on systems you do not own | Target URLs must pass an explicit allowlist. |

If you need any of the above, NEXUS is the wrong tool and you should stop.

## 3. The four modules

```
                      +---------------------------+
                      |   Task Manager & Analytics |  coordinates everything
                      +-------------+-------------+
                                    |
        +---------------+-----------+-----------+---------------+
        |               |                       |               |
+-------v------+ +------v-------+      +--------v-------+ +-----v--------+
| Proxy Manager| | Email Manager|      | Registration   | |  Dashboard   |
|              | |              |      | Lab            | |  & Reports   |
+--------------+ +--------------+      +----------------+ +--------------+
  your network     test inboxes          authorized          numbers and
  paths            (Mailpit)             sign-up flows       exports
```

### Module A — Proxy Manager
Import, normalize, deduplicate and **health-check** HTTP/HTTPS/SOCKS5 proxies that you own or
are authorized to use. Records latency and reliability, exports profiles to CSV/JSON.
*Never* connected to the registration engine.

### Module B — Email Manager
Creates random test inbox addresses on a reserved local domain, receives messages via SMTP into
Mailpit, exposes them through an API, extracts verification codes, and expires inboxes
automatically. *Never* talks to a third-party mail provider.

### Module C — Registration Lab
Adapter-based engine that runs sign-up scenarios against an allowlisted target (the bundled
demo app by default). Receives the verification email from Mailpit, extracts the code, completes
verification, and asserts the resulting account state. Stops and reports on 429/CAPTCHA/denial.

### Module D — Task Manager & Analytics
Background job queue with states (`queued → running → completed/failed/cancelled`), progress,
logs, safe cancellation, duration tracking, and CSV/JSON reporting.

## 4. Who it is for

- **Primary user:** a developer or QA engineer who owns the target application.
- **Learning context:** this project is also a teaching vehicle. See
  [LEARNING_GUIDE.md](LEARNING_GUIDE.md) for the concepts introduced at each phase.

## 5. Success criteria (MVP)

The MVP is done when a new developer can clone the repository, run one command to install and
one command to start, and then complete this flow end-to-end against the bundled demo app:

```
create inbox  ->  run registration campaign  ->  code received & extracted
              ->  account verified           ->  report exported to CSV
```

...with automated tests covering the critical path, and documentation sufficient to run it
without asking the author.

## 6. Non-functional requirements

| Requirement | Target |
|---|---|
| Local-first | Must run on a developer laptop with no cloud dependency |
| Zero-infrastructure mode | Must start with SQLite + in-process queue (no Docker) |
| Responsiveness | Desktop UI must never freeze during background work |
| Security | Secrets via env vars; SSRF guard; audit log; no plaintext credentials |
| Portability | Windows/macOS/Linux; PowerShell commands documented for Windows |
| Maintainability | Modules communicate through service interfaces, not shared tables |

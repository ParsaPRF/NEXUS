# demo_target/

A **complete, self-contained demonstration registration application** that NEXUS may test
against. It is the default target when no external staging application has been authorized.

```
POST /api/register  ->  202 pending_verification, emails a 6-digit code
POST /api/verify    ->  200 active  (400 invalid code, 410 expired)
GET  /api/status/{email} -> missing | pending_verification | active
```

Run it:

```powershell
cd ..\backend          # the backend virtualenv provides FastAPI
uv run python -m demo_target.run
# -> http://127.0.0.1:9000  (loopback only)
```

## Why it exists

The brief requires that registration testing only ever run against applications the owner
controls or is explicitly authorized to test. Shipping a demo target makes the *correct* default
also the *easy* one: you need nobody's permission to run the full flow against it.

## Design

| Property | How | Why |
|---|---|---|
| Separate application | `demo_target/app.py` imports nothing from `app.*` | A target must behave like a target, not like part of NEXUS |
| Own account store | `DemoStore`, in memory | It is disposable; no migrations, no leftover state, a clean slate per process |
| Own password hashing | PBKDF2-SHA256, salted | Independent of NEXUS's Argon2 setup; no cross-imports |
| Mail via an injectable mailer | `Mailer` protocol: `SmtpMailer` (default, Mailpit on loopback) or `CallbackMailer` (tests) | In tests the mail goes straight into NEXUS's in-memory backend, so the whole loop runs with no SMTP |
| Codes | 6 digits, 10-minute TTL, 5 attempts | Realistic enough that the extraction and failure paths are meaningful |
| Simulation switch | `POST /api/_simulate` with `rate_limit` / `captcha` / `access_denied` / `slow` | Makes "stop on an anti-abuse control" **testable**; an untestable requirement is not a requirement |

## Why not SQLite

The repository-level design note originally assumed its own SQLite file. The implementation uses
an in-memory store instead: the application is disposable by definition, tests get perfect
isolation from constructing a new instance, and there is no schema to migrate. If a future phase
needs the demo state to survive restarts, that is a small, contained change — and it would be
recorded as a decision rather than done quietly.

## Safety

- Binds `127.0.0.1` only.
- Its default mailer submits to Mailpit on loopback, which cannot deliver to the internet.
- The `_simulate` endpoint exists only to test refusal handling; it is excluded from the OpenAPI
  schema and is not a bypass of anything.

## Tests

`backend/tests/integration/test_demo_target.py` drives it as a black box over HTTP, and
`backend/tests/integration/test_registration_flow.py` runs the full NEXUS → demo → mail → verify
loop in-process.

## See also

- [ROADMAP.md](../docs/ROADMAP.md) — Phase 4
- [SECURITY.md](../docs/SECURITY.md) §3 — the ownership rules this target makes testable
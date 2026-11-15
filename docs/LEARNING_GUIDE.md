# NEXUS — Learning Guide

NEXUS is a teaching vehicle as much as a product. This guide maps **what you learn** to **where
you can see it in the code**, phase by phase. Read the section for the phase you are working on
rather than all of it at once.

Each entry has the same shape:

- **Concept** — the idea, in plain language
- **Why it exists here** — the concrete problem it solves in NEXUS
- **Where to look** — the file that demonstrates it
- **Try it** — an exercise that makes the idea stick

---

## Phase 0 — Architecture thinking

### Modular monolith

**Concept.** One deployable program, internally divided into modules with strict rules about who
may talk to whom. Unlike microservices, there is no network between modules — the boundaries are
enforced by convention and by code review.

**Why here.** Four modules (proxies, email, registration, tasks) must evolve independently, but
running five services on one laptop would be absurd. This is the cheapest structure that keeps
the option to split later.

**Where.** [ARCHITECTURE.md](ARCHITECTURE.md) §4, [DECISION_LOG.md](DECISION_LOG.md) D1.

**Try it.** Read the module table in ARCHITECTURE §4 and answer: if the Registration Lab needed
inbox data, which file would it import? (Answer: `email_manager/service.py`, never
`email_manager/models.py`.)

### Architectural Decision Records (ADRs)

**Concept.** A short written record of a decision: the context, the choice, the alternatives you
rejected, and the consequences. Cheap to write, enormously valuable two months later when you
ask "why is this like this?".

**Why here.** Without it, the SQLite default looks like laziness instead of a deliberate response
to a missing Docker daemon.

**Where.** [DECISION_LOG.md](DECISION_LOG.md).

**Try it.** Write an ADR for a decision in your own project. Keep it under 20 lines.

### The difference between a requirement and a mechanism

**Concept.** "Don't do X" is a requirement. A mechanism is what makes doing X impossible.

**Why here.** [SECURITY.md](SECURITY.md) §3 maps every prohibition to a mechanism: no third-party
mailboxes → a single Mailpit adapter on a reserved domain; no proxy rotation in registration →
the proxy parameter does not exist.

**Try it.** For each row of that table, ask "if a future contributor wanted to violate this,
what would they have to change?" If the answer is "nothing, it just works", the mechanism is not
real.

---

## Phase 1 — Foundation concepts

### Application factory + dependency injection

**Concept.** Instead of building one global application object, a function (`create_app`) builds
it from parameters. FastAPI's `Depends` then injects pieces (settings, database session, current
user) into route functions instead of those functions reaching out for globals.

**Why here.** Tests build an app with throwaway settings; production builds it from environment
variables. Same code, different wiring.

**Where.** `app/main.py::create_app`, `app/api/v1/meta.py` (the `Depends` usage).

**Watch out.** Routes declare `Depends(get_settings)`, which resolves the cached singleton. The
factory overrides that dependency so a test-built app reads *its own* settings. Missing this
override caused a real bug in Phase 1 (the test saw production defaults) — a good illustration
of why "it works in tests" needs the right tests.

### pydantic-settings: configuration as a typed object

**Concept.** Environment variables are strings. You want typed, validated configuration with
defaults, and an error at startup if something required is missing.

**Why here.** `NEXUS_NETWORK__ALLOWED_TARGET_HOSTS` is a JSON array in the environment and a real
`list[str]` in code. A malformed value fails at startup, not at 3 a.m. during a campaign.

**Where.** `app/core/config.py`.

**Try it.** Set `NEXUS_NETWORK__MAX_CONCURRENT_CHECKS=0` and start the app. Read the validation
error. Then set it to `abc` and read that one too.

### Structured logging with a redaction filter

**Concept.** Logs are for humans; structured logs (JSON) are for machines. A `logging.Filter`
sits between the logger and the handler and can rewrite records.

**Why here.** A log line is a leak vector. `logger.info("token=%s", token)` stores the secret in
`record.args`; a filter that only cleans `record.msg` misses it entirely.

**Where.** `app/core/logging.py`, `RedactionFilter.filter`.

**The subtle bug to understand.** A filter can run more than once per record (multiple handlers),
so redaction must be **idempotent**. An early version of the regex left stray whitespace on a
second pass; the fix was a fixed-width lookbehind. This is a genuinely instructive bug.

**Try it.** Read `tests/unit/test_logging_redaction.py::test_lazy_formatting_arguments_are_redacted`
and then try to write a variant that would have passed with the *broken* implementation. (You
can't — that's the point.)

### SSRF, and why a deny-list is not enough

**Concept.** Server-Side Request Forgery: an attacker makes your server fetch a URL of their
choosing, reaching things only the server can see (localhost services, LAN hosts, cloud metadata).

**Why here.** Users supply proxy source URLs, a check URL, and target URLs. Every one is a
potential attack.

**Layers of defence, in order of subtlety:**

1. Block non-HTTP schemes (`file://`, `gopher://`).
2. Block hostnames meaning "local" (`localhost`, `*.local`, `metadata.google.internal`).
3. Block literal IP ranges (loopback, private, link-local, multicast, reserved).
4. **Re-check after DNS resolution** — otherwise `evil.example.com` resolving to `127.0.0.1`
   walks straight through. This is DNS rebinding.
5. Handle IPv4-mapped IPv6 (`::ffff:127.0.0.1`) — loopback in disguise.

**Where.** `app/core/net_guard.py`, tests in `tests/unit/test_net_guard.py`.

**Try it.** Add a test for `http://[::ffff:169.254.169.254]/` and confirm it is blocked.

### Async I/O

**Concept.** `async`/`await` lets one thread handle thousands of waiting operations (network
calls) by switching tasks instead of blocking. It is concurrency without threads.

**Why here.** Checking 500 proxies is 500 network waits. Sequentially that is minutes; with
`asyncio.gather` and a `Semaphore` cap it is seconds, and the cap stops you from becoming a
denial-of-service yourself.

**Where.** `app/database/engine.py` (async engine), `app/jobs/queue.py` (tasks + semaphore).

**The trap.** Async is contagious: one blocking call inside an async function stalls the whole
loop. Use `await asyncio.to_thread(...)` for genuinely blocking work.

**Try it.** In a scratch script, time 20 `asyncio.sleep(0.1)` calls with a `for` loop vs
`asyncio.gather`. Then add a `Semaphore(5)`.

### SQLAlchemy 2.x: models, sessions, and transactions

**Concept.** An ORM maps Python classes to tables. A `Session` is a unit of work: changes are
staged until `commit()`, and `rollback()` discards them.

**Why here.** Fourteen tables with real relationships. Explicit transactions mean a failed
campaign step does not leave half-written rows.

**Where.** `app/models/*`, `app/database/engine.py`.

**Key ideas to notice:**

- `Mapped[int]` + `mapped_column(...)` — typed attributes, checked by your editor.
- `server_default` vs `default` — the database fills the value at INSERT, so an object built in
  memory but not yet flushed has `None`, not `0`. This caused real test failures; the fix was to
  treat `None` defensively in computed properties.
- `expire_on_commit=False` — needed so a committed object can still be read without a lazy load.
- `pool_pre_ping=True` — silently recovers a dropped connection.

**Try it.** In `tests/integration/test_models.py`, find a test that depends on the database and
one that does not. Explain the difference.

### Constraints are documentation that the database enforces

**Concept.** `UNIQUE`, `FOREIGN KEY ... ON DELETE CASCADE`, `NOT NULL` — each one encodes a rule
that no application bug can violate.

**Why here.**

- `UNIQUE(inbox_id, backend_message_id)` makes message ingestion idempotent: a retried poll
  cannot duplicate messages.
- `UNIQUE(registration_tests.campaign_id, email)` stops a re-run from silently creating a second
  test for the same identity.
- `ON DELETE CASCADE` on `messages.inbox_id` cleans up children; `ON DELETE RESTRICT` on
  `inboxes.domain_id` refuses to orphan them.

**Where.** `app/models/registration_models.py`, `app/models/email_models.py`,
[DB design](DATABASE_DESIGN.md) §3.

**Try it.** Delete a domain that has inboxes in a scratch test and read the error. Then explain
why RESTRICT is the right choice there and CASCADE is right for messages.

**SQLite gotcha.** Foreign keys are **not enforced** unless `PRAGMA foreign_keys=ON` is issued per
connection. `create_engine` registers a connect listener to do exactly that; without it, every
`ON DELETE` rule in the schema would silently do nothing.

### Alembic: migrations as version control for the schema

**Concept.** Each schema change is a numbered Python file with `upgrade()` and `downgrade()`. The
`alembic_version` table records where the database is.

**Why here.** A teammate's database must reach the same structure as yours without manual SQL.

**Where.** `alembic/env.py`, `alembic/versions/0001_initial.py`.

**Two things that commonly go wrong:**

1. A model that is never imported is missing from `Base.metadata`, so autogenerate thinks its
   table is unnecessary and may emit `DROP TABLE`. `alembic/env.py` imports `app.models` for
   exactly this reason.
2. SQLite cannot `ALTER` most columns, so batch mode rewrites the table. That is why adding the
   cross-module foreign keys uses `batch_alter_table`.

**Try it.** `uv run alembic upgrade head`, then `uv run alembic downgrade base`, then
`uv run python scripts/check_migration.py nexus.db` and watch it fail (the tables are gone).

### Password hashing vs encryption

**Concept.** *Hashing* is one-way: you can verify a password but never recover it. *Encryption*
is two-way: you can get the original back. Use each only where it fits.

**Why here.**

| Data | Technique | Why |
|---|---|---|
| Local user password | Argon2id hash | Never needs to be recovered |
| Proxy password | Fernet encryption | Must be replayed to the proxy, so hashing is impossible |

If no credential key is configured, NEXUS **refuses to store** the proxy rather than keeping the
password in plain text.

**Where.** `app/core/security.py`.

**Try it.** Hash the same password twice and compare — the hashes differ (random salt). Then
encrypt the same value twice — also different (random IV), but both decrypt to the original.

### Constant-time comparison

**Concept.** `a == b` on secret strings can leak information through how long it takes to fail.
`hmac.compare_digest` always takes the same time.

**Why here.** API key comparison.

**Where.** `app/core/security.py::_constant_time_equals`.

### Unit vs integration vs end-to-end tests

**Concept.**

- **Unit** — pure logic, no I/O, milliseconds. `test_net_guard.py`.
- **Integration** — real components wired together (API + database), still local. `test_models.py`.
- **End-to-end** — the real deployed stack (Postgres, Redis, Mailpit, browser). Slow, brittle,
  but the only proof the whole thing works.

**Why here.** The first two tiers must run with no services so `pytest` is always meaningful;
tier three is marked `e2e` and skipped by default.

**Where.** `tests/`, `pyproject.toml` markers.

**Try it.** Add an `e2e`-marked test and confirm `uv run pytest` skips it while
`uv run pytest -m e2e` tries to run it.

### Fixtures and isolation

**Concept.** A pytest fixture is setup/teardown around a test. `yield`-based fixtures clean up
after themselves.

**Why here.** Each test gets its own database file, its own settings object and its own job
queue, so test order cannot change a result.

**Where.** `tests/conftest.py`.

**Try it.** Deliberately break isolation: reuse one settings object across two tests and see
which assertions start failing.

### Cooperative cancellation

**Concept.** You cannot safely kill a thread in the middle of a transaction. Instead, the job
checks a flag at safe points and stops itself. A fallback cancel exists for jobs that never
check.

**Why here.** The Task Manager must cancel a running campaign without corrupting its progress.

**Where.** `app/jobs/queue.py`, `JobContext.check_cancelled`, `InProcessJobQueue.cancel`.

**Try it.** Read `test_a_job_that_never_checks_cannot_block_shutdown_forever`. It waits 5
seconds, then force-cancels. Explain why the test asserts the status is `cancelled` *after*
`await cancel(...)` returns — and why the implementation had to await the task for that to hold.

### Idempotency

**Concept.** An operation is idempotent if running it twice has the same effect as running it
once. Essential for anything that can be retried.

**Why here.** A crashed worker re-runs a job; a poller asks Mailpit twice. Neither may duplicate
data.

**Where.** `background_jobs.idempotency_key` (unique), `messages(inbox_id, backend_message_id)`
(unique).

**Try it.** Insert the same message twice in a scratch test and observe the `IntegrityError`.

### The adapter pattern (coming in Phases 3–5)

**Concept.** Define the behaviour you need as an interface, then implement it per backend. Code
depends on the interface.

**Why here.** Mailpit today, something else later; RQ later. `MailBackend` and `JobQueue` are the
seams. The registration adapter is also where the "stop on 429/CAPTCHA" contract lives.

**Where.** `app/jobs/queue.py::JobQueue` (already a `Protocol`), and the planned
`email_manager/backends.py`.

---

## Coming in later phases — what to study when you get there

| Phase | Concepts |
|---|---|
| 2 | Parsing untrusted text safely; de-duplication keys; bounded concurrency (`Semaphore`); measuring latency correctly; soft delete vs hard delete |
| 3 | SMTP basics; idempotent ingestion; regex extraction with confidence scoring; TTL/expiry sweeps; why HTML email must never be rendered |
| 4 | Adapter pattern in practice; allowlisting; Playwright and accessibility selectors; classifying failures vs controls; stopping conditions |
| 5 | Redis and RQ; worker processes; progress reporting; report generation; retention/pruning jobs |
| 6 | Qt event loop vs asyncio (never mix); Qt signals/slots; model/view; keeping the GUI thread free |
| 7 | Threat-model review; dependency auditing; reproducible builds; honest verification (only claim what you ran) |

---

## The single most important habit this project teaches

**Do not claim something works because it looks right.**

Every phase report must state what was *executed* and what the *actual result* was. In Phase 1
that meant running `pytest` (258 passed), running `alembic upgrade head` and then inspecting the
resulting SQLite file, and starting uvicorn and calling `/health` for real. It also meant writing
down that Docker Compose is **unverified**, because the daemon was not running.

"Should work" is not a test result.
# NEXUS — Decision Log (ADR-lite)

Every architecturally significant decision is recorded here with its context, the choice made,
the alternatives rejected, and the consequences. Format is deliberately lightweight: one entry
per decision, newest last.

**Status values:** `Accepted` · `Superseded` · `Proposed`

---

## D1 — Modular monolith, single deployable package

- **Status:** Accepted (Phase 0)
- **Context:** Four modules with clear responsibilities. Options were (a) one monolith with
  directory boundaries, (b) microservices per module, (c) a shared library plus separate apps.
- **Decision:** One FastAPI application; each module is a package under
  `app/modules/<name>/` owning its own models, schemas, services and router. Cross-module
  communication goes through a module's **service interface**, never through its tables.
- **Alternatives rejected:**
  - *Microservices* — needs service discovery, network error handling, distributed tracing and
    a message broker before producing any user value. Massive overengineering for a single
    developer on a laptop.
  - *Shared library* — hides boundaries but forces a rebuild for every change; no benefit here.
- **Consequences:** (+) Simple to run and debug, one process, one migration history.
  (−) Boundaries are enforced by discipline and review, not by the network. Mitigated by an
  import-direction rule documented in `ARCHITECTURE.md` §6.
- **When to revisit:** If a module needs independent scaling or a separate release cadence.

---

## D2 — Adapter pattern at every external boundary

- **Status:** Accepted (Phase 0)
- **Context:** NEXUS touches three replaceable external systems: the mail backend (Mailpit
  today, something else later), the registration target (bundled demo app today, an authorized
  staging app later), and the job queue (in-process today, Redis/RQ later).
- **Decision:** Each boundary is defined by a Python `Protocol` / abstract base class with a
  concrete implementation behind it, selected by configuration.
- **Alternatives rejected:** Calling Mailpit's HTTP API and RQ directly from business logic —
  fastest to write, and guarantees a rewrite the first time a dependency changes.
- **Consequences:** (+) Swappable components; the demo target and Mailpit are usable in tests
  without containers. (+) The "stop on CAPTCHA/429" contract has a single place to live.
  (−) One extra indirection layer per boundary. Accepted: it is small and pays for itself in
  Phase 5 when the queue is swapped.
- **Also matters for safety:** There is no third-party-mail-provider adapter to add later
  without an explicit, visible architectural decision — which is the point.

---

## D3 — Zero-infrastructure default: SQLite + in-process queue

- **Status:** Accepted (Phase 0) — **environment-driven**
- **Context:** The technical assessment established that **the Docker daemon is not running**
  on the development machine, and ports 5432/6379/8025 are closed. The brief requires
  PostgreSQL, Redis and Mailpit.
- **Decision:** Configuration has two profiles:
  - **`local` (default):** SQLite via `aiosqlite`, in-process job runner, mock/eager mail
    backend. `uv run pytest` and `uv run uvicorn ...` work with nothing else installed.
  - **`docker`/`staging`:** PostgreSQL via async driver, Redis + RQ worker, Mailpit SMTP.
  Switching is a change of environment variables only — no code change.
- **Alternatives rejected:**
  - *Require Postgres+Redis from day one* — blocks all progress until Docker Desktop runs, and
    makes the test suite slow and flaky.
  - *Mocks everywhere, no real drivers* — would leave the Postgres/Redis path unverified and
    drift from reality. Rejected: the drivers are real, only the default URL differs.
- **Consequences:** (+) The project is buildable and testable today. (+) Fast hermetic tests.
  (−) Two database dialects to keep compatible. Mitigated by restricting models to portable
  SQLAlchemy types and running migrations against SQLite in CI. PostgreSQL verification is an
  explicit Phase 7 acceptance item and stays **unverified** until then.

---

## D4 — Async-first backend

- **Status:** Accepted (Phase 0)
- **Context:** The dominant workload is network I/O: health-checking hundreds of proxies,
  polling a mail backend, driving HTTP registration steps.
- **Decision:** FastAPI + SQLAlchemy 2.x async + HTTPX `AsyncClient` throughout the backend.
- **Alternatives rejected:** Threads — works, but a thread-per-proxy model means manual pool
  sizing and harder structured cancellation. Sync + `asyncio.to_thread` — leaks blocking calls
  through the stack.
- **Consequences:** (+) Thousands of concurrent health checks with bounded concurrency.
  (+) Clean cancellation for the Task Manager. (−) Async is contagious: mixing blocking calls
  requires `asyncio.to_thread`. Documented in the development guide.
- **PySide6 interaction:** Qt owns its own event loop, so the desktop app must **not** run an
  asyncio loop in the GUI thread. It calls the REST API over HTTP instead (see D7).

---

## D5 — Secrets strictly via environment variables

- **Status:** Accepted (Phase 0)
- **Decision:** All secrets come from the environment (or a git-ignored `.env` used only for
  local development and loaded by pydantic-settings). Settings are validated **at import
  time**; missing required secrets abort startup with a clear message. `.env.example` contains
  placeholders only.
- **Alternatives rejected:** A committed config file with defaults — leaks secrets into git
  history, which is irreversible.
- **Consequences:** (+) Nothing sensitive is ever committed. (+) Fail-fast misconfiguration.
  (−) Slightly more setup friction, mitigated by `.env.example` + docs.
- **Related:** Logging redacts secret-shaped values (D-redaction in `core/logging.py`).

---

## D6 — SSRF guard on every user-configurable URL

- **Status:** Accepted (Phase 0)
- **Context:** Users supply proxy import source URLs, a health-check URL, and registration
  target URLs. The server fetches them. That is the textbook definition of an SSRF sink, and on
  a developer laptop `169.254.169.254` and `127.0.0.1:5432` are reachable.
- **Decision:** A single `core/net_guard.py` validates every outbound user-supplied URL:
  scheme allowlist (`http`/`https`), host deny-list (loopback, private, link-local, multicast,
  unspecified, IPv6 equivalents), blocked metadata endpoints, **DNS resolution followed by an
  IP-range re-check** (defeats DNS-rebinding to a private address), and a response-size cap.
  The registration allowlist is enforced by the same module.
- **Alternatives rejected:** Relying on documentation ("don't enter bad URLs") — not a control.
- **Consequences:** (+) Private-network pivoting and metadata theft are blocked at one choke
  point that tests can cover exhaustively. (−) Legitimate local targets during development need
  an explicit opt-in setting, which is off by default.

---

## D7 — Desktop UI is a pure API client

- **Status:** Accepted (Phase 0)
- **Decision:** The PySide6 application performs **no** database access and contains **no**
  business logic. It calls the FastAPI backend over HTTP and renders results.
- **Alternatives rejected:** Embedding SQLAlchemy models in Qt code — fast at first, then every
  UI change risks corrupting data, and the web-migration path dies.
- **Consequences:** (+) Backend fully testable without a GUI. (+) Future web UI is a new client,
  not a rewrite. (−) Requires the backend to be running, and needs an error path for "backend
  unavailable" — which is correct behaviour anyway.

---

## D8 — Ownership and authorization constraints are architectural

- **Status:** Accepted (Phase 0)
- **Context:** The brief forbids third-party mailbox creation, proxy rotation in registration,
  and testing against non-authorized systems. Good intentions do not survive feature pressure.
- **Decision:** Enforce each prohibition with a mechanism, not a comment:
  - Email Manager ships only a Mailpit adapter bound to a locally reserved domain.
  - Registration target URLs pass an explicit allowlist check.
  - Registration code has **no** proxy parameter at all (absent, not merely ignored).
  - 429 / CAPTCHA / access-denied are terminal outcomes in the adapter contract.
  - Imported files are parsed as inert data; no `eval`/`exec`/`pickle` on user input.
- **Consequences:** (+) The unsafe configuration is not reachable by a config change or a
  copy-paste from a tutorial. (−) Some legitimate-sounding requests (e.g. "let registration use
  the proxy pool") require a deliberate, documented architecture change — which is the desired
  friction.

---

## D9 — Test strategy: hermetic by default, integrated on demand

- **Status:** Accepted (Phase 0)
- **Decision:** Three test tiers, only the first two required for a green build:
  1. **Unit** — pure logic (parsers, guards, code extraction). No I/O.
  2. **Integration** — API + database against SQLite, in-memory mail backend. Fast, hermetic.
  3. **End-to-end** — real Postgres/Redis/Mailpit/demo target via Docker Compose and Playwright.
     Marked `e2e`, skipped unless explicitly requested (`-m e2e`).
- **Alternatives rejected:** Requiring the full stack for every test run — slow, flaky, and on
  this machine currently impossible.
- **Consequences:** (+) `uv run pytest` is always meaningful. (−) Real-infrastructure paths are
  only exercised on demand, so the Phase 7 checklist must run tier 3 explicitly.

---

## D10 — Package layout: `backend/` and `desktop/` as separate distributions

- **Status:** Accepted (Phase 0)
- **Decision:** The brief's layout is kept, with two clarifications: (1) the Python package is
  `backend/app` and is the uv project root at `backend/`; (2) `desktop/` is a *consumer* of the
  backend's HTTP API and declares no dependency on `backend/app`, preventing accidental
  shortcut imports.
- **Deviation from the brief:** `demo_target/` lives at the repository root (it is a standalone
  application, not part of the backend package) and `docs/` gained `ROADMAP.md` and
  `LEARNING_GUIDE.md`.
- **Consequences:** (+) Import boundaries are physically enforced by packaging. (−) Two
  dependency declarations to maintain; worth it to keep Qt out of the server install.

---

## D11 — Alembic from Phase 1, before there is anything to migrate

- **Status:** Accepted (Phase 0)
- **Context:** It is tempting to add migrations once "the schema settles". Schemas never settle.
- **Decision:** Configure Alembic in Phase 1 and generate the initial revision as soon as the
  first models exist. Every later model change requires a revision.
- **Consequences:** (+) Any database state is reproducible from scratch. (+) Catches
  SQLite/Postgres dialect differences early. (−) A small ceremony per schema change.

---

## D12 — The proxy check URL is trusted configuration, not user input

- **Status:** Accepted (Phase 2)
- **Context:** The SSRF guard (D6) exists because users supply URLs. A proxy health check also
  needs a target URL, but it is chosen by the operator in `.env`, not typed into a request.
- **Decision:** `NEXUS_PROXY__CHECK_URL` is treated as trusted configuration: it is not subjected
  to the private-address block, because the honest default is *this application's own* `/health`
  route on loopback. What is enforced instead is the ownership rule: the check endpoint must be a
  service the operator runs. NEXUS does not grade proxies against third-party services.
- **Alternatives rejected:**
  - *Require the allowlist for the check URL too* — the allowlist gates **registration targets**,
    a different trust boundary. Conflating them would force operators to allowlist their own
    application just to check a proxy, and invite them to widen the registration allowlist for
    the wrong reason.
  - *Run the check URL through the SSRF guard as-is* — would block the loopback default and push
    operators toward pointing checks at an external service, which is the behaviour we do not
    want.
- **Consequences:** (+) Checking works out of the box with no third-party traffic.
  (−) An operator *can* misconfigure it to hit a third-party service. Documented and warned
  against in `.env.example`, the API reference and this entry.

---

## D13 — Test seam: injectable HTTP transport, proxy omitted when injected

- **Status:** Accepted (Phase 2)
- **Context:** Health-check classification (timeout vs refusal vs TLS vs proxy auth) needs
  tests. Opening real sockets to a real proxy would make tests flaky and would also mean the
  suite depends on network access.
- **Decision:** `ProxyHealthChecker` accepts an `httpx.AsyncBaseTransport`. When a transport is
  injected, the proxy is **not** passed to the client.
- **Why not pass both:** httpx converts `proxy=` into its own proxy transport and ignores any
  transport supplied alongside it. Passing both silently made the checker attempt real
  connections — which surfaced as eight failing tests and multi-second stalls, not as an error.
- **Consequences:** (+) Every failure mode is testable in milliseconds with no network.
  (−) The seam bypasses proxy routing, so it tests *classification*, not proxy plumbing; proxy
  URL construction is covered separately by `TestProxyUrlBuilding`.

---

## D14 — Mailpit owns SMTP; NEXUS polls it

- **Status:** Accepted (Phase 3)
- **Context:** Test inboxes need to receive mail. Two shapes were possible: (a) NEXUS runs its own
  SMTP listener and stores messages itself, or (b) Mailpit receives SMTP and NEXUS reads it.
- **Decision:** Mailpit owns the SMTP port and the mailbox store; NEXUS reads messages through
  its HTTP API, on demand (``POST /inboxes/{id}/poll``) or via the ``inbox.poll_all`` job.
- **Alternatives rejected:**
  - *Own SMTP listener* — duplicates Mailpit's job, creates two stores of truth, and means
    re-implementing MIME parsing and storage limits. Mailpit already does both well.
  - *Mailpit webhooks* — would require NEXUS to accept inbound pushes, adding an authenticated
    listener and a retry/idempotency story for no benefit at this scale.
- **Consequences:** (+) One source of truth for received mail; ingestion is idempotent and
  explicitly timed, which is what the tests want. (+) The in-memory backend implements the same
  protocol, so the whole module is testable with no container. (−) Delivery is not instantaneous;
  a poll interval bounds it. Acceptable for QA flows, where the runner polls anyway.

---

## D15 — A "demo" target must be local; that is why it needs no allowlist entry

- **Status:** Accepted (Phase 4)
- **Context:** External targets require two gates (stored authorization **and** a host allowlist).
  The bundled demonstration application would otherwise be unusable out of the box, because a
  fresh instance has an empty allowlist - which is also the correct default.
- **Decision:** Targets with ``kind = "demo"`` are exempt from the external allowlist **only if
  their host is loopback** (``127.0.0.1``, ``localhost``, ``::1``). The exemption is enforced at
  target creation *and* re-checked by the executor before a run.
- **Alternatives rejected:**
  - *Allowlist ``127.0.0.1`` by default* — would also permit every other local service the
    operator runs, including databases and admin panels. Too broad a hole for a convenience.
  - *Require the operator to allowlist the demo* — makes the safe path the annoying one, and
    people respond to that by widening the allowlist.
- **Consequences:** (+) ``curl``-free demo works immediately. (+) The exemption cannot be turned
  into a bypass: pointing ``kind: demo`` at a public host is refused with a 403, which the tests
  assert. (−) A demo target on a LAN address is not supported; it must be reconfigured as an
  external target with a proper note.

---

## D16 — API adapter only in Phase 4; Playwright deferred deliberately

- **Status:** Accepted (Phase 4)
- **Context:** The brief lists Playwright "only when browser-level UI testing is required". The
  bundled demo target exposes a documented JSON API and has no HTML UI.
- **Decision:** Ship the API adapter. Do **not** ship a Playwright adapter this phase.
- **Alternatives rejected:** *Write the Playwright adapter anyway* — it would drive nothing, and
  nothing would test it. Untested code that claims a capability is worse than an absent feature
  with a documented reason.
- **Consequences:** (+) Every shipped adapter is exercised by the end-to-end test.
  (−) A UI-only authorized target cannot be tested yet; the ``RegistrationAdapter`` protocol is the
  seam where that adapter will attach. Recorded in ROADMAP Phase 4 as the explicit gap.

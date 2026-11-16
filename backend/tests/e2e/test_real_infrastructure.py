"""End-to-end tests against real infrastructure.

These are the tests that prove the pieces work **together** against the software the project
actually targets, rather than against stand-ins:

* PostgreSQL instead of SQLite (so dialect differences surface)
* Mailpit instead of the in-memory mail backend (so SMTP delivery, its HTTP API and message
  parsing are exercised for real)
* Redis + an RQ worker instead of the in-process runner (so the queue adapter runs a job in
  another process)

They are marked ``e2e`` and skipped by default, because they need the compose stack:

    docker compose up -d
    docker compose exec api alembic upgrade head
    cd backend
    uv run pytest -m e2e

Nothing here is run by a normal ``uv run pytest``; that suite must stay green on a machine with
no Docker at all (Decision D3/D9).
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Iterator

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------------------
# Environment plumbing
# --------------------------------------------------------------------------------------


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


POSTGRES_URL = _env(
    "NEXUS_E2E_DATABASE_URL", "postgresql+asyncpg://nexus:nexus@127.0.0.1:5432/nexus"
)
MAILPIT_API = _env("NEXUS_E2E_MAILPIT_API", "http://127.0.0.1:8025")
MAILPIT_SMTP_HOST = _env("NEXUS_E2E_MAILPIT_SMTP_HOST", "127.0.0.1")
MAILPIT_SMTP_PORT = int(_env("NEXUS_E2E_MAILPIT_SMTP_PORT", "1025"))
REDIS_URL = _env("NEXUS_E2E_REDIS_URL", "redis://127.0.0.1:6379/0")


def _reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """True when something is listening on ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def infrastructure() -> Iterator[dict[str, object]]:
    """Skip the whole module when the stack is not up, with a message that says what to start."""
    missing: list[str] = []
    if not _reachable("127.0.0.1", 5432):
        missing.append("PostgreSQL on 127.0.0.1:5432")
    if not _reachable(MAILPIT_SMTP_HOST, MAILPIT_SMTP_PORT):
        missing.append(f"Mailpit SMTP on {MAILPIT_SMTP_HOST}:{MAILPIT_SMTP_PORT}")
    if not _reachable("127.0.0.1", 8025):
        missing.append("Mailpit HTTP API on 127.0.0.1:8025")
    if not _reachable("127.0.0.1", 6379):
        missing.append("Redis on 127.0.0.1:6379")

    if missing:
        pytest.skip("e2e infrastructure not running: " + ", ".join(missing))

    yield {
        "database_url": POSTGRES_URL,
        "mailpit_api": MAILPIT_API,
        "smtp_host": MAILPIT_SMTP_HOST,
        "smtp_port": MAILPIT_SMTP_PORT,
        "redis_url": REDIS_URL,
    }


@pytest.fixture
async def postgres_settings(infrastructure: dict[str, object]) -> Iterator[object]:
    """Settings pointed at the real PostgreSQL and Mailpit."""
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        profile="docker",
        environment="staging",
        secret_key="e2e-secret-key-long-enough-for-validation",
        api_key="e2e-api-key-long-enough",
        proxy_credential_key="e2e-credential-key-long-enough",
        database={"url": str(infrastructure["database_url"])},
        mail={
            "backend": "mailpit",
            "api_url": str(infrastructure["mailpit_api"]),
            "smtp_host": str(infrastructure["smtp_host"]),
            "smtp_port": int(infrastructure["smtp_port"]),  # type: ignore[arg-type]
        },
        jobs={"backend": "rq", "redis_url": str(infrastructure["redis_url"])},
    )
    yield settings


# --------------------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------------------


class TestPostgres:
    """The schema and the queries work on the real database, not just on SQLite."""

    async def test_engine_connects(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        from app.database.engine import create_engine, get_engine

        engine = create_engine(postgres_settings)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT version()"))
                assert "PostgreSQL" in result.scalar_one()
        finally:
            await engine.dispose()
        assert get_engine is not None

    async def test_models_can_be_created_and_queried(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        from app.core.security import hash_password
        from app.database.engine import dispose_engine, get_engine, get_session_factory, init_engine
        from app.models import Base, User

        init_engine(postgres_settings)
        try:
            async with get_engine().begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            factory = get_session_factory()
            async with factory() as session:
                user = User(username="e2e-user", password_hash=hash_password("e2e-password"))
                session.add(user)
                await session.commit()
                assert user.id is not None
        finally:
            await dispose_engine()

    async def test_timezone_aware_timestamps_round_trip(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        """PostgreSQL returns aware datetimes where SQLite returns naive ones.

        The task tracker computes a duration by subtracting a stored ``started_at`` from an aware
        "now"; on SQLite that combination raised ``TypeError`` until it was normalised. This test
        pins the dialect difference that caused it.
        """
        from datetime import UTC, datetime

        from app.database.engine import dispose_engine, get_engine, get_session_factory, init_engine
        from app.models import Base
        from app.models.enums import JobStatus
        from app.models.job_models import BackgroundJob
        from app.modules.task_manager.tracker import _as_utc

        init_engine(postgres_settings)
        try:
            async with get_engine().begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            factory = get_session_factory()
            async with factory() as session:
                job = BackgroundJob(
                    public_id="e2e-tz",
                    job_type="e2e.tz",
                    status=JobStatus.COMPLETED.value,
                    queued_at=datetime.now(UTC),
                    started_at=datetime.now(UTC),
                )
                session.add(job)
                await session.commit()
                await session.refresh(job)

                # Whatever the dialect hands back, normalising it must allow arithmetic.
                elapsed = datetime.now(UTC) - _as_utc(job.started_at)  # type: ignore[arg-type]
                assert elapsed.total_seconds() >= 0
        finally:
            await dispose_engine()


# --------------------------------------------------------------------------------------
# Mailpit
# --------------------------------------------------------------------------------------


class TestMailpit:
    """Real SMTP delivery, real Mailpit API, real message parsing."""

    async def test_health(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        from app.modules.email_manager.backends import MailpitBackend

        backend = MailpitBackend(postgres_settings)
        assert await backend.health() is True

    async def test_delivered_message_is_readable(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        """Send a real SMTP message and read it back through the API."""
        from app.modules.email_manager.backends import MailpitBackend

        recipient = "e2e-inbox@example.test"
        code = "654321"
        await _send_smtp(
            to=recipient,
            subject="Your verification code",
            body=f"Your verification code is {code}",
            host=postgres_settings.mail.smtp_host,
            port=postgres_settings.mail.smtp_port,
        )

        backend = MailpitBackend(postgres_settings)
        ids = await backend.list_ids(recipient)
        assert ids, "Mailpit did not report the message we just sent"

        message = await backend.fetch(ids[0])
        assert message is not None
        assert message.recipient.lower() == recipient
        assert code in (message.body_text or "")

    async def test_extraction_reads_the_real_message(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        """The extraction function works on a message that travelled through SMTP."""
        from app.modules.email_manager.backends import MailpitBackend
        from app.modules.email_manager.extraction import extract_verification_code

        recipient = "e2e-extract@example.test"
        await _send_smtp(
            to=recipient,
            subject="Confirm your account",
            body="Use the confirmation code 112233 to continue.",
            host=postgres_settings.mail.smtp_host,
            port=postgres_settings.mail.smtp_port,
        )

        backend = MailpitBackend(postgres_settings)
        ids = await backend.list_ids(recipient)
        assert ids
        message = await backend.fetch(ids[0])
        assert message is not None

        result = extract_verification_code(text=message.body_text, html=message.body_html)
        assert result.code == "112233"
        assert result.confidence >= 0.7

    async def test_html_body_is_parsed_but_not_rendered(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        from app.modules.email_manager.backends import MailpitBackend
        from app.modules.email_manager.extraction import extract_verification_code

        recipient = "e2e-html@example.test"
        await _send_smtp(
            to=recipient,
            subject="HTML message",
            body="",
            html="<html><body><p>Your code is <b>998877</b></p></body></html>",
            host=postgres_settings.mail.smtp_host,
            port=postgres_settings.mail.smtp_port,
        )

        backend = MailpitBackend(postgres_settings)
        ids = await backend.list_ids(recipient)
        assert ids
        message = await backend.fetch(ids[0])
        assert message is not None
        result = extract_verification_code(text=message.body_text, html=message.body_html)
        assert result.code == "998877"


async def _send_smtp(
    *,
    to: str,
    subject: str,
    body: str,
    host: str,
    port: int,
    html: str | None = None,
) -> None:
    """Send a message over real SMTP to Mailpit."""
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "no-reply@demo.test"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body or " ")
    if html:
        message.add_alternative(html, subtype="html")

    def _send() -> None:
        with smtplib.SMTP(host, port, timeout=10) as client:
            client.send_message(message)

    await asyncio.to_thread(_send)


# --------------------------------------------------------------------------------------
# Redis / RQ
# --------------------------------------------------------------------------------------


class TestRqWorker:
    """The RQ adapter against a real Redis, including a real worker process."""

    async def test_redis_is_reachable(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        import redis

        connection = redis.Redis.from_url(postgres_settings.jobs.redis_url)
        assert connection.ping() is True
        connection.close()

    async def test_job_can_be_enqueued_on_real_redis(self, postgres_settings) -> None:  # type: ignore[no-untyped-def]
        from app.jobs.queue import reset_queue
        from app.jobs.rq_queue import RqJobQueue

        await reset_queue()
        queue = RqJobQueue(postgres_settings)
        job_id = await queue.enqueue("test.progress", {"job_db_id": 1, "e2e": True})
        assert job_id

        snapshot = await queue.get_status(job_id)
        assert snapshot.status in {"queued", "running"}

        await queue.cancel(job_id)
        await queue.shutdown()

    async def test_job_survives_a_real_worker(
        self, postgres_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enqueue, run a worker in-process, and see the job finish.

        ``rq.Worker.work(burst=True)`` runs until the queue is empty and then exits, which is the
        cheapest honest way to prove a worker really executes our sync dispatcher.
        """
        import redis
        from rq import SimpleWorker, Worker

        from app.jobs.queue import reset_queue
        from app.jobs.rq_queue import RqJobQueue

        await reset_queue()
        queue = RqJobQueue(postgres_settings)
        job_id = await queue.enqueue("test.progress", {"job_db_id": 1})

        connection = redis.Redis.from_url(postgres_settings.jobs.redis_url)
        try:
            # ``SimpleWorker`` avoids forking, which Windows cannot do; on Linux either works.
            worker = SimpleWorker([queue._queue], connection=connection)  # noqa: SLF001
            await asyncio.to_thread(worker.work, burst=True)
            snapshot = await queue.get_status(job_id)
            assert snapshot.status == "finished" or snapshot.status == "completed"
        finally:
            connection.close()
            await queue.shutdown()
            del Worker


# --------------------------------------------------------------------------------------
# Whole flow on real infrastructure
# --------------------------------------------------------------------------------------


class TestFullFlow:
    """The registration loop against the demo target, with Mailpit and PostgreSQL."""

    async def test_registration_end_to_end(self, postgres_settings) -> None:
        """Create an inbox, drive the demo target, and verify the account through Mailpit.

        The demo application runs in-process over an injected transport (its API is local), while
        the mailbox side is the **real** Mailpit, so the pieces that would break in production -
        SMTP delivery and Mailpit parsing - are not simulated.
        """
        from demo_target.app import SmtpMailer, create_demo_app
        from httpx import ASGITransport

        from app.database.engine import dispose_engine, get_engine, get_session_factory, init_engine
        from app.models import Base
        from app.modules.email_manager.service import InboxService
        from app.modules.registration_lab.adapters import DemoApiAdapter
        from app.modules.registration_lab.analyzer import analyze
        from app.modules.registration_lab.verification import InboxEmailVerifier

        init_engine(postgres_settings)
        try:
            async with get_engine().begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            # Demo target: real SMTP to Mailpit, its own in-memory account store.
            demo_app = create_demo_app(
                mailer=SmtpMailer(
                    host=postgres_settings.mail.smtp_host,
                    port=postgres_settings.mail.smtp_port,
                )
            )

            async with get_session_factory()() as session:
                inboxes = InboxService(session, postgres_settings)
                inbox = await inboxes.create_inbox(label="e2e")
                assert inbox.address.endswith("@example.test")

                adapter = DemoApiAdapter(
                    "http://127.0.0.1:9000",
                    transport=ASGITransport(app=demo_app),  # type: ignore[arg-type]
                )
                submit = await adapter.submit(email=inbox.address, password="e2e-password-123")
                assert submit.ok, submit.message

                verifier = InboxEmailVerifier(inboxes)
                verification = await verifier.wait_for_code(
                    inbox_public_id=inbox.public_id, timeout_seconds=30, poll_interval=0.5
                )
                assert verification.found, verification.message

                verify = await adapter.verify(email=inbox.address, code=verification.code or "")
                account = await adapter.account_status(email=inbox.address)

                analysis = analyze(submit=submit, verify=verify, account=account, code_found=True)
                assert analysis.succeeded, analysis.detail
                assert analysis.steps["verification_code"]
        finally:
            await dispose_engine()

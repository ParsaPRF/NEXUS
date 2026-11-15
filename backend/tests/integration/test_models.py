"""Database and model integration tests.

These run against a real (if temporary) SQLite database rather than mocks, because the
properties worth checking are the ones only a database can demonstrate: that foreign keys are
enforced, that a cascade actually deletes children, that a unique constraint rejects a
duplicate, and that the whole schema can be created in one go.

The suite also pins the schema against ``docs/DATABASE_DESIGN.md``: if a table is added or
renamed, the table-count and name assertions here fail, which forces the design document to be
updated at the same time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models import (
    AuditLog,
    BackgroundJob,
    Base,
    Campaign,
    EmailDomain,
    Inbox,
    JobLog,
    Message,
    Proxy,
    ProxyCheck,
    ProxySource,
    RegistrationResult,
    RegistrationTarget,
    RegistrationTest,
    User,
)
from app.models.enums import (
    AuditOutcome,
    CampaignStatus,
    InboxStatus,
    JobStatus,
    ProxyProtocol,
    ProxyStatus,
    ResultOutcome,
    TargetKind,
)
from app.models.enums import (
    TestStatus as RegistrationTestStatus,
)

#: The fourteen tables from docs/DATABASE_DESIGN.md 3.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "audit_logs",
        "proxy_sources",
        "proxies",
        "proxy_checks",
        "email_domains",
        "inboxes",
        "messages",
        "registration_targets",
        "campaigns",
        "registration_tests",
        "registration_results",
        "background_jobs",
        "job_logs",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------


class TestSchema:
    """The physical schema matches the documented design."""

    async def test_every_documented_table_exists(self, db_engine: object) -> None:
        async with db_engine.connect() as connection:  # type: ignore[attr-defined]
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert names >= EXPECTED_TABLES

    async def test_no_undocumented_tables(self, db_engine: object) -> None:
        # Guards against a stray table created by a typo'd __tablename__.
        async with db_engine.connect() as connection:  # type: ignore[attr-defined]
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert names - EXPECTED_TABLES == set()

    async def test_foreign_keys_are_declared_for_every_child_column(
        self, db_engine: object
    ) -> None:
        # A missing foreign key means an orphaned row is possible; this checks the important
        # relationships explicitly rather than trusting a visual review of the models.
        expected: dict[str, set[str]] = {
            "proxies": {"source_id"},
            "proxy_checks": {"proxy_id"},
            "inboxes": {"domain_id", "campaign_id"},
            "messages": {"inbox_id"},
            "campaigns": {"target_id", "created_by", "job_id"},
            "registration_tests": {"campaign_id", "inbox_id"},
            "registration_results": {"test_id"},
            "job_logs": {"job_id"},
            "audit_logs": {"user_id"},
        }

        async with db_engine.connect() as connection:  # type: ignore[attr-defined]

            def _collect(sync_connection):  # type: ignore[no-untyped-def]
                inspector = inspect(sync_connection)
                return {
                    table: {
                        fk["constrained_columns"][0] for fk in inspector.get_foreign_keys(table)
                    }
                    for table in inspector.get_table_names()
                }

            actual = await connection.run_sync(_collect)

        for table, columns in expected.items():
            assert actual[table] >= columns, f"{table} is missing a foreign key on {columns}"

    async def test_sqlite_enforces_foreign_keys(self, session: AsyncSession) -> None:
        # SQLite ignores foreign keys unless the pragma is enabled; the engine enables it, and
        # this test is what proves that wiring actually took effect.
        from sqlalchemy import text

        result = await session.execute(text("PRAGMA foreign_keys"))
        assert result.scalar() == 1


# --------------------------------------------------------------------------------------
# Module A: proxies
# --------------------------------------------------------------------------------------


class TestProxyModels:
    """Proxy storage, identity uniqueness and reliability maths."""

    async def test_proxy_can_be_stored_and_read(self, session: AsyncSession) -> None:
        session.add(Proxy(protocol=ProxyProtocol.SOCKS5, host="proxy.example.test", port=1080))
        await session.commit()

        stored = (await session.execute(select(Proxy))).scalar_one()
        assert stored.host == "proxy.example.test"
        assert stored.status == ProxyStatus.UNKNOWN.value
        assert stored.display_address == "socks5://proxy.example.test:1080"

    async def test_duplicate_endpoint_and_account_is_rejected(self, session: AsyncSession) -> None:
        session.add(Proxy(protocol="http", host="dup.example.test", port=8080, username="bob"))
        await session.commit()

        session.add(Proxy(protocol="http", host="dup.example.test", port=8080, username="bob"))
        # The database is the last line of defence: even if the importer's application-level
        # de-duplication were removed, this must not produce a second row.
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_same_endpoint_with_different_account_is_allowed(
        self, session: AsyncSession
    ) -> None:
        session.add_all(
            [
                Proxy(protocol="http", host="shared.example.test", port=8080, username="alice"),
                Proxy(protocol="http", host="shared.example.test", port=8080, username="carol"),
            ]
        )
        await session.commit()
        count = (await session.execute(select(func.count()).select_from(Proxy))).scalar_one()
        assert count == 2

    async def test_reliability_is_none_before_any_check(self) -> None:
        # An in-memory proxy that has never been checked has no reliability at all. Returning
        # 0.0 would be a lie: it would look like it always failed.
        proxy = Proxy(protocol="http", host="fresh.example.test", port=8080)
        assert proxy.success_count is None  # no INSERT has applied the default yet
        assert proxy.reliability is None

    async def test_reliability_is_the_success_ratio(self, session: AsyncSession) -> None:
        session.add(
            Proxy(
                protocol="http",
                host="measured.example.test",
                port=8080,
                success_count=3,
                failure_count=1,
            )
        )
        await session.commit()

        proxy = (await session.execute(select(Proxy))).scalar_one()
        assert proxy.reliability == pytest.approx(0.75)

    async def test_deleting_a_proxy_cascades_to_its_checks(self, session: AsyncSession) -> None:
        proxy = Proxy(protocol="http", host="cascade.example.test", port=8080)
        session.add(proxy)
        await session.commit()

        session.add_all(
            [
                ProxyCheck(proxy_id=proxy.id, checked_at=_now(), ok=True, latency_ms=120),
                ProxyCheck(proxy_id=proxy.id, checked_at=_now(), ok=False, error_class="timeout"),
            ]
        )
        await session.commit()
        assert (
            await session.execute(select(func.count()).select_from(ProxyCheck))
        ).scalar_one() == 2

        await session.delete(proxy)
        await session.commit()
        assert (
            await session.execute(select(func.count()).select_from(ProxyCheck))
        ).scalar_one() == 0

    async def test_deleting_a_source_preserves_its_proxies(self, session: AsyncSession) -> None:
        # SET NULL, not CASCADE: removing the record of where a list came from must not
        # silently delete proxies the user may still rely on. Attribution loss is visible,
        # data loss is not.
        source = ProxySource(name="import", kind="file", imported_at=_now())
        session.add(source)
        await session.commit()
        session.add(Proxy(source_id=source.id, protocol="http", host="keep.example.test", port=80))
        await session.commit()

        await session.delete(source)
        await session.commit()

        stored = (await session.execute(select(Proxy))).scalar_one()
        assert stored.source_id is None

    async def test_proxy_source_records_import_counts(self, session: AsyncSession) -> None:
        source = ProxySource(
            name="list.txt",
            kind="file",
            imported_at=_now(),
            entry_count=100,
            accepted_count=93,
            rejected_count=7,
        )
        session.add(source)
        await session.commit()

        stored = (await session.execute(select(ProxySource))).scalar_one()
        assert stored.entry_count == stored.accepted_count + stored.rejected_count


# --------------------------------------------------------------------------------------
# Module B: email
# --------------------------------------------------------------------------------------


class TestEmailModels:
    """Inbox addresses, message idempotency and the domain safety rule."""

    async def _domain(self, session: AsyncSession) -> EmailDomain:
        domain = EmailDomain(domain="example.test", is_reserved=True)
        session.add(domain)
        await session.commit()
        return domain

    async def test_inbox_address_is_unique(self, session: AsyncSession) -> None:
        domain = await self._domain(session)
        session.add(
            Inbox(
                public_id=str(uuid.uuid4()),
                domain_id=domain.id,
                local_part="same",
                address="same@example.test",
            )
        )
        await session.commit()

        session.add(
            Inbox(
                public_id=str(uuid.uuid4()),
                domain_id=domain.id,
                local_part="same",
                address="same@example.test",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_reserved_domain_is_usable(self, session: AsyncSession) -> None:
        assert (await self._domain(session)).is_usable

    async def test_unverified_unreserved_domain_is_not_usable(self, session: AsyncSession) -> None:
        # This property is the data-level enforcement of "no third-party mailboxes".
        domain = EmailDomain(domain="real-company.com", is_reserved=False, owner_verified=False)
        assert not domain.is_usable

    async def test_owner_verified_domain_is_usable(self, session: AsyncSession) -> None:
        domain = EmailDomain(
            domain="mine.example.org",
            is_reserved=False,
            owner_verified=True,
            is_active=True,
        )
        session.add(domain)
        await session.commit()
        assert domain.is_usable is True

    async def test_inactive_domain_is_not_usable(self, session: AsyncSession) -> None:
        domain = EmailDomain(domain="example.test", is_reserved=True, is_active=False)
        session.add(domain)
        await session.commit()
        assert domain.is_usable is False

    async def test_message_ingestion_is_idempotent(self, session: AsyncSession) -> None:
        # The unique constraint on (inbox_id, backend_message_id) is what makes a retried poll
        # safe. Without it, a worker retry would duplicate every message it had already stored.
        domain = await self._domain(session)
        inbox = Inbox(
            public_id=str(uuid.uuid4()),
            domain_id=domain.id,
            local_part="idem",
            address="idem@example.test",
        )
        session.add(inbox)
        await session.commit()

        payload = {
            "inbox_id": inbox.id,
            "backend_message_id": "mailpit-1",
            "sender": "no-reply@demo.test",
            "recipient": "idem@example.test",
            "subject": "Your code",
            "received_at": _now(),
        }
        session.add(Message(**payload))
        await session.commit()

        session.add(Message(**payload))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_identical_backend_ids_in_different_inboxes_are_allowed(
        self, session: AsyncSession
    ) -> None:
        domain = await self._domain(session)
        first = Inbox(
            public_id=str(uuid.uuid4()),
            domain_id=domain.id,
            local_part="a",
            address="a@example.test",
        )
        second = Inbox(
            public_id=str(uuid.uuid4()),
            domain_id=domain.id,
            local_part="b",
            address="b@example.test",
        )
        session.add_all([first, second])
        await session.commit()

        for inbox in (first, second):
            session.add(
                Message(
                    inbox_id=inbox.id,
                    backend_message_id="shared-id",
                    sender="s@example.test",
                    recipient=inbox.address,
                    received_at=_now(),
                )
            )
        await session.commit()
        assert (await session.execute(select(func.count()).select_from(Message))).scalar_one() == 2

    async def test_deleting_an_inbox_cascades_to_messages(self, session: AsyncSession) -> None:
        domain = await self._domain(session)
        inbox = Inbox(
            public_id=str(uuid.uuid4()),
            domain_id=domain.id,
            local_part="gone",
            address="gone@example.test",
        )
        session.add(inbox)
        await session.commit()
        session.add(
            Message(
                inbox_id=inbox.id,
                backend_message_id="m1",
                sender="s@example.test",
                recipient=inbox.address,
                received_at=_now(),
            )
        )
        await session.commit()

        await session.delete(inbox)
        await session.commit()
        assert (await session.execute(select(func.count()).select_from(Message))).scalar_one() == 0

    async def test_a_domain_with_inboxes_cannot_be_deleted(self, session: AsyncSession) -> None:
        # RESTRICT: deleting the domain would orphan every inbox on it.
        domain = await self._domain(session)
        session.add(
            Inbox(
                public_id=str(uuid.uuid4()),
                domain_id=domain.id,
                local_part="kept",
                address="kept@example.test",
            )
        )
        await session.commit()

        await session.delete(domain)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


# --------------------------------------------------------------------------------------
# Module C: registration
# --------------------------------------------------------------------------------------


class TestRegistrationModels:
    """The authorization gates and the test/result shape."""

    async def _target(self, session: AsyncSession, **overrides: object) -> RegistrationTarget:
        defaults: dict[str, object] = {
            "name": "Bundled demo application",
            "base_url": "http://127.0.0.1:9000",
            "kind": TargetKind.DEMO,
            "adapter_name": "demo",
            "is_authorized": True,
            "authorization_note": "Owned by the project owner; used as the bundled demo target.",
        }
        defaults.update(overrides)
        target = RegistrationTarget(**defaults)  # type: ignore[arg-type]
        session.add(target)
        await session.commit()
        return target

    async def test_unauthorized_target_may_not_be_tested(self, session: AsyncSession) -> None:
        target = await self._target(session, is_authorized=False, authorization_note=None)
        assert target.may_be_tested is False

    async def test_authorized_target_without_a_note_may_not_be_tested(
        self, session: AsyncSession
    ) -> None:
        # Both gates are required. "Someone said it was fine" is not a record.
        target = await self._target(session, is_authorized=True, authorization_note=None)
        assert target.may_be_tested is False

    async def test_authorized_and_documented_target_may_be_tested(
        self, session: AsyncSession
    ) -> None:
        assert (await self._target(session)).may_be_tested is True

    async def test_inactive_target_may_not_be_tested(self, session: AsyncSession) -> None:
        target = await self._target(session, is_active=False)
        assert target.may_be_tested is False

    async def test_base_url_is_unique(self, session: AsyncSession) -> None:
        await self._target(session)
        session.add(
            RegistrationTarget(
                name="Duplicate",
                base_url="http://127.0.0.1:9000",
                kind=TargetKind.DEMO,
                adapter_name="demo",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_a_target_with_campaigns_cannot_be_deleted(self, session: AsyncSession) -> None:
        target = await self._target(session)
        session.add(
            Campaign(
                public_id=str(uuid.uuid4()),
                name="Run 1",
                target_id=target.id,
                status=CampaignStatus.QUEUED,
            )
        )
        await session.commit()

        await session.delete(target)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_duplicate_identity_in_one_campaign_is_rejected(
        self, session: AsyncSession
    ) -> None:
        target = await self._target(session)
        campaign = Campaign(public_id=str(uuid.uuid4()), name="Run", target_id=target.id)
        session.add(campaign)
        await session.commit()

        session.add(
            RegistrationTest(
                campaign_id=campaign.id,
                email="dup@example.test",
                status=RegistrationTestStatus.PENDING,
            )
        )
        await session.commit()

        # Re-running a campaign must not silently create a second test for one identity.
        session.add(
            RegistrationTest(
                campaign_id=campaign.id,
                email="dup@example.test",
                status=RegistrationTestStatus.PENDING,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_campaign_progress_is_computed_safely(self, session: AsyncSession) -> None:
        target = await self._target(session)
        campaign = Campaign(public_id=str(uuid.uuid4()), name="Progress", target_id=target.id)
        session.add(campaign)
        await session.commit()
        await session.refresh(campaign)

        # No tests yet: a naive divide would raise ZeroDivisionError.
        assert campaign.progress_percent == 0.0

        campaign.total_tests = 4
        campaign.completed_tests = 1
        assert campaign.progress_percent == 25.0

    async def test_progress_is_capped_at_one_hundred(self) -> None:
        campaign = Campaign(
            public_id="x", name="Over", target_id=1, total_tests=2, completed_tests=99
        )
        assert campaign.progress_percent == 100.0

    async def test_one_result_per_test(self, session: AsyncSession) -> None:
        target = await self._target(session)
        campaign = Campaign(public_id=str(uuid.uuid4()), name="R", target_id=target.id)
        session.add(campaign)
        await session.commit()
        test = RegistrationTest(campaign_id=campaign.id, email="one@example.test")
        session.add(test)
        await session.commit()

        session.add(RegistrationResult(test_id=test.id, outcome=ResultOutcome.SUCCESS))
        await session.commit()

        session.add(RegistrationResult(test_id=test.id, outcome=ResultOutcome.EMAIL_TIMEOUT))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_control_outcomes_are_classified_as_terminal(self) -> None:
        # This classification is what tells the campaign executor to STOP rather than retry.
        assert ResultOutcome.RATE_LIMITED.is_control_encountered
        assert ResultOutcome.CAPTCHA_ENCOUNTERED.is_control_encountered
        assert ResultOutcome.ACCESS_DENIED.is_control_encountered

    async def test_product_defects_are_not_classified_as_controls(self) -> None:
        assert not ResultOutcome.VERIFICATION_FAILED.is_control_encountered
        assert not ResultOutcome.EMAIL_TIMEOUT.is_control_encountered
        assert not ResultOutcome.VALIDATION_ERROR.is_control_encountered
        assert not ResultOutcome.SUCCESS.is_control_encountered


# --------------------------------------------------------------------------------------
# Module D: jobs
# --------------------------------------------------------------------------------------


class TestJobModels:
    """Job identity, idempotency and progress."""

    async def test_idempotency_key_is_unique(self, session: AsyncSession) -> None:
        # A retried job must not be able to claim the same key twice.
        session.add(
            BackgroundJob(
                public_id=str(uuid.uuid4()),
                job_type="proxy.check_batch",
                idempotency_key="batch-1",
                queued_at=_now(),
            )
        )
        await session.commit()

        session.add(
            BackgroundJob(
                public_id=str(uuid.uuid4()),
                job_type="proxy.check_batch",
                idempotency_key="batch-1",
                queued_at=_now(),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_multiple_jobs_may_have_no_idempotency_key(self, session: AsyncSession) -> None:
        # NULLs are distinct in a unique index, so unrelated jobs coexist.
        session.add_all(
            [
                BackgroundJob(
                    public_id=str(uuid.uuid4()),
                    job_type="a",
                    idempotency_key=None,
                    queued_at=_now(),
                ),
                BackgroundJob(
                    public_id=str(uuid.uuid4()),
                    job_type="b",
                    idempotency_key=None,
                    queued_at=_now(),
                ),
            ]
        )
        await session.commit()
        assert (
            await session.execute(select(func.count()).select_from(BackgroundJob))
        ).scalar_one() == 2

    async def test_progress_percentage(self, session: AsyncSession) -> None:
        job = BackgroundJob(
            public_id=str(uuid.uuid4()),
            job_type="t",
            queued_at=_now(),
            progress_current=3,
            progress_total=4,
        )
        assert job.progress_percent == 75.0

    async def test_progress_without_a_total_is_zero(self, session: AsyncSession) -> None:
        session.add(BackgroundJob(public_id=str(uuid.uuid4()), job_type="t", queued_at=_now()))
        await session.commit()

        job = (await session.execute(select(BackgroundJob))).scalar_one()
        assert job.progress_percent == 0.0

    async def test_terminal_states_are_identified(self) -> None:
        assert JobStatus.COMPLETED.is_terminal
        assert JobStatus.FAILED.is_terminal
        assert JobStatus.CANCELLED.is_terminal
        assert not JobStatus.QUEUED.is_terminal
        assert not JobStatus.RUNNING.is_terminal

    async def test_deleting_a_job_cascades_to_its_logs(self, session: AsyncSession) -> None:
        job = BackgroundJob(public_id=str(uuid.uuid4()), job_type="t", queued_at=_now())
        session.add(job)
        await session.commit()
        session.add(JobLog(job_id=job.id, level="info", message="started"))
        await session.commit()

        await session.delete(job)
        await session.commit()
        assert (await session.execute(select(func.count()).select_from(JobLog))).scalar_one() == 0


# --------------------------------------------------------------------------------------
# Core: users and the audit trail
# --------------------------------------------------------------------------------------


class TestUserAndAuditModels:
    """User storage hashes passwords, and the audit trail is append-only."""

    async def test_password_is_stored_hashed(self, session: AsyncSession) -> None:
        plaintext = "a-real-password"
        session.add(User(username="owner", password_hash=hash_password(plaintext)))
        await session.commit()

        stored = (await session.execute(select(User))).scalar_one()
        assert stored.password_hash != plaintext
        assert stored.password_hash.startswith("$argon2id$")

    async def test_username_is_unique(self, session: AsyncSession) -> None:
        session.add(User(username="owner", password_hash=hash_password("password-one")))
        await session.commit()
        session.add(User(username="owner", password_hash=hash_password("password-two")))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_audit_record_round_trips(self, session: AsyncSession) -> None:
        session.add(
            AuditLog(
                action="proxy.import",
                outcome=AuditOutcome.SUCCESS,
                detail_json={"accepted": 10, "rejected": 2},
                created_at=_now(),
            )
        )
        await session.commit()

        stored = (await session.execute(select(AuditLog))).scalar_one()
        assert stored.action == "proxy.import"
        assert stored.detail_json == {"accepted": 10, "rejected": 2}

    async def test_deleting_a_user_keeps_audit_records(self, session: AsyncSession) -> None:
        # SET NULL, deliberately: the audit trail must survive the deletion of the actor, or
        # offboarding a user would erase the record of what they did.
        user = User(username="temp", password_hash=hash_password("some-password"))
        session.add(user)
        await session.commit()
        session.add(
            AuditLog(action="target.authorize", outcome=AuditOutcome.SUCCESS, created_at=_now())
        )
        # Link via the relationship to prove the FK is populated.
        session.add(
            AuditLog(
                user_id=user.id,
                action="campaign.start",
                outcome=AuditOutcome.SUCCESS,
                created_at=_now(),
            )
        )
        await session.commit()

        await session.delete(user)
        await session.commit()

        stored = (await session.execute(select(AuditLog))).scalars().all()
        assert len(stored) == 2
        assert all(record.user_id is None for record in stored)

    async def test_audit_records_can_be_filtered_by_entity(self, session: AsyncSession) -> None:
        session.add_all(
            [
                AuditLog(
                    action="target.authorize",
                    entity_type="registration_target",
                    entity_id="1",
                    outcome=AuditOutcome.SUCCESS,
                    created_at=_now(),
                ),
                AuditLog(
                    action="target.authorize",
                    entity_type="registration_target",
                    entity_id="2",
                    outcome=AuditOutcome.DENIED,
                    created_at=_now(),
                ),
                AuditLog(
                    action="proxy.import",
                    entity_type="proxy_source",
                    entity_id="1",
                    outcome=AuditOutcome.SUCCESS,
                    created_at=_now(),
                ),
            ]
        )
        await session.commit()

        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == "registration_target", AuditLog.entity_id == "2"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].outcome == AuditOutcome.DENIED


# --------------------------------------------------------------------------------------
# Cross-cutting
# --------------------------------------------------------------------------------------


class TestEnumStorage:
    """Enums are stored as readable strings, not opaque integers."""

    async def test_values_are_stored_as_text(self, session: AsyncSession) -> None:
        from sqlalchemy import text

        session.add(Proxy(protocol=ProxyProtocol.SOCKS5, host="text.example.test", port=1080))
        await session.commit()

        raw = (await session.execute(text("SELECT protocol, status FROM proxies"))).one()
        # Readable in the database and in exported reports, which is why StrEnum was chosen.
        assert raw[0] == "socks5"
        assert raw[1] == "unknown"


class TestInboxLifecycle:
    """Expiry bookkeeping used by the cleanup job."""

    async def test_expires_at_supports_the_cleanup_query(self, session: AsyncSession) -> None:
        domain = EmailDomain(domain="example.test", is_reserved=True)
        session.add(domain)
        await session.commit()

        expired = Inbox(
            public_id=str(uuid.uuid4()),
            domain_id=domain.id,
            local_part="old",
            address="old@example.test",
            status=InboxStatus.ACTIVE,
            expires_at=_now() - timedelta(hours=1),
        )
        current = Inbox(
            public_id=str(uuid.uuid4()),
            domain_id=domain.id,
            local_part="new",
            address="new@example.test",
            status=InboxStatus.ACTIVE,
            expires_at=_now() + timedelta(hours=1),
        )
        session.add_all([expired, current])
        await session.commit()

        due = (
            (
                await session.execute(
                    select(Inbox).where(
                        Inbox.status == InboxStatus.ACTIVE, Inbox.expires_at < _now()
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [inbox.local_part for inbox in due] == ["old"]


def test_all_models_are_registered_in_metadata() -> None:
    """Every documented table must exist in ``Base.metadata``.

    This is the guard for the Alembic pitfall described in ``app/models/__init__.py``: a model
    that is never imported is absent from the metadata, and autogenerate would then treat its
    table as unnecessary - potentially emitting a DROP TABLE.
    """
    assert set(Base.metadata.tables) == EXPECTED_TABLES

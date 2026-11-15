"""Initial schema: every NEXUS table.

Revision ID: 0001_initial
Revises: (none)
Create Date: 2026-11-15

This revision creates the complete Phase 1 schema - all fourteen tables from
docs/DATABASE_DESIGN.md. It is intentionally one revision rather than fourteen: there is no
existing deployment to migrate incrementally, and a single reviewed file is easier to read than
a chain of artificial steps.

Constraint and index names match the naming convention in ``app/database/base.py`` exactly. That
matters: ``Base.metadata`` is the source of truth Alembic compares against, and a mismatched
name would make autogenerate propose a spurious drop/create on the next revision.

Portability note: no PostgreSQL-specific types are used, so the same revision produces a valid
SQLite database (the local profile) and a valid PostgreSQL database (the docker profile).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the initial schema."""
    _create_core_tables()
    _create_proxy_tables()
    _create_email_tables()
    _create_registration_tables()
    _create_job_tables()
    _add_cross_module_links()


def _create_core_tables() -> None:
    """users and audit_logs.

    Created first because ``audit_logs.user_id`` and ``campaigns.created_by`` reference ``users``.
    """
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column(
            "password_hash",
            sa.String(length=255),
            nullable=False,
            comment="Argon2id hash. A plaintext password is never stored.",
        ),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="admin"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False, server_default="success"),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column("client_ip", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_audit_logs_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"], unique=False)
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"], unique=False)
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)
    op.create_index(
        "ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"], unique=False
    )


def _create_proxy_tables() -> None:
    """Module A: proxy_sources, proxies, proxy_checks."""
    op.create_table(
        "proxy_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proxy_sources"),
    )

    op.create_table(
        "proxies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("protocol", sa.String(length=8), nullable=False, server_default="http"),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column(
            "password_encrypted",
            sa.String(length=512),
            nullable=True,
            comment="Reversibly encrypted with NEXUS_PROXY_CREDENTIAL_KEY. Never plain text.",
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["proxy_sources.id"],
            name="fk_proxies_source_id_proxy_sources",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proxies"),
        # Same endpoint AND same account = the same proxy. NULL usernames do not collide in SQL,
        # so the importer also de-duplicates in application code (see models/proxy_models.py).
        sa.UniqueConstraint(
            "protocol", "host", "port", "username", name="uq_proxies_endpoint_identity"
        ),
    )
    op.create_index("ix_proxies_source_id", "proxies", ["source_id"], unique=False)
    op.create_index("ix_proxies_status", "proxies", ["status"], unique=False)
    op.create_index("ix_proxies_status_latency", "proxies", ["status", "latency_ms"], unique=False)

    op.create_table(
        "proxy_checks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proxy_id", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["proxy_id"],
            ["proxies.id"],
            name="fk_proxy_checks_proxy_id_proxies",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proxy_checks"),
    )
    op.create_index("ix_proxy_checks_proxy_id", "proxy_checks", ["proxy_id"], unique=False)
    op.create_index("ix_proxy_checks_checked_at", "proxy_checks", ["checked_at"], unique=False)
    op.create_index(
        "ix_proxy_checks_proxy_checked", "proxy_checks", ["proxy_id", "checked_at"], unique=False
    )


def _create_email_tables() -> None:
    """Module B: email_domains, inboxes, messages.

    The ``inboxes.campaign_id`` foreign key is added later in :func:`_add_cross_module_links`,
    because ``campaigns`` does not exist yet at this point.
    """
    op.create_table(
        "email_domains",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("is_reserved", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("owner_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_domains"),
        sa.UniqueConstraint("domain", name="uq_email_domains_domain"),
    )
    op.create_index("ix_email_domains_domain", "email_domains", ["domain"], unique=True)

    op.create_table(
        "inboxes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("domain_id", sa.Integer(), nullable=False),
        sa.Column("local_part", sa.String(length=64), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["email_domains.id"],
            name="fk_inboxes_domain_id_email_domains",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inboxes"),
        sa.UniqueConstraint("domain_id", "local_part", name="uq_inboxes_domain_local_part"),
        sa.UniqueConstraint("address", name="uq_inboxes_address"),
    )
    op.create_index("ix_inboxes_public_id", "inboxes", ["public_id"], unique=True)
    op.create_index("ix_inboxes_domain_id", "inboxes", ["domain_id"], unique=False)
    op.create_index("ix_inboxes_address", "inboxes", ["address"], unique=True)
    op.create_index("ix_inboxes_status", "inboxes", ["status"], unique=False)
    op.create_index("ix_inboxes_expires_at", "inboxes", ["expires_at"], unique=False)
    op.create_index("ix_inboxes_campaign_id", "inboxes", ["campaign_id"], unique=False)
    op.create_index(
        "ix_inboxes_status_expires_at", "inboxes", ["status", "expires_at"], unique=False
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("inbox_id", sa.Integer(), nullable=False),
        sa.Column("backend_message_id", sa.String(length=255), nullable=True),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column(
            "body_html",
            sa.Text(),
            nullable=True,
            comment="Stored but never rendered: an email body is untrusted input.",
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("extracted_code", sa.String(length=32), nullable=True),
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
        sa.Column("headers_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["inboxes.id"],
            name="fk_messages_inbox_id_inboxes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        # Idempotent ingestion: polling the mail backend twice cannot duplicate a message.
        # Caveat: SQL treats NULLs as distinct, so rows with a NULL backend_message_id are not
        # de-duplicated by the database. The ingestion service always sets this value.
        sa.UniqueConstraint("inbox_id", "backend_message_id", name="uq_messages_inbox_backend_id"),
    )
    op.create_index("ix_messages_inbox_id", "messages", ["inbox_id"], unique=False)
    op.create_index(
        "ix_messages_backend_message_id", "messages", ["backend_message_id"], unique=False
    )
    op.create_index("ix_messages_received_at", "messages", ["received_at"], unique=False)
    op.create_index(
        "ix_messages_inbox_received", "messages", ["inbox_id", "received_at"], unique=False
    )


def _create_registration_tables() -> None:
    """Module C: registration_targets, campaigns, registration_tests, registration_results."""
    op.create_table(
        "registration_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "base_url",
            sa.String(length=512),
            nullable=False,
            comment="Validated by the SSRF guard and the host allowlist before any request.",
        ),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="demo"),
        sa.Column("adapter_name", sa.String(length=64), nullable=False),
        sa.Column(
            "is_authorized",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Defaults to FALSE so a forgotten flag fails closed.",
        ),
        sa.Column("authorization_note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_registration_targets"),
        sa.UniqueConstraint("base_url", name="uq_registration_targets_base_url"),
    )

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("total_tests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_tests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_tests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_tests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("scenario_config_json", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["registration_targets.id"],
            name="fk_campaigns_target_id_registration_targets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_campaigns_created_by_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaigns"),
    )
    op.create_index("ix_campaigns_public_id", "campaigns", ["public_id"], unique=True)
    op.create_index("ix_campaigns_target_id", "campaigns", ["target_id"], unique=False)
    op.create_index("ix_campaigns_status", "campaigns", ["status"], unique=False)

    op.create_table(
        "registration_tests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("inbox_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "password_ref",
            sa.String(length=128),
            nullable=True,
            comment="Reference to a generated secret, never the secret itself.",
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_registration_tests_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["inbox_id"],
            ["inboxes.id"],
            name="fk_registration_tests_inbox_id_inboxes",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_registration_tests"),
        sa.UniqueConstraint("campaign_id", "email", name="uq_registration_tests_campaign_email"),
    )
    op.create_index(
        "ix_registration_tests_campaign_id", "registration_tests", ["campaign_id"], unique=False
    )
    op.create_index(
        "ix_registration_tests_inbox_id", "registration_tests", ["inbox_id"], unique=False
    )
    op.create_index("ix_registration_tests_status", "registration_tests", ["status"], unique=False)
    op.create_index(
        "ix_registration_tests_campaign_status",
        "registration_tests",
        ["campaign_id", "status"],
        unique=False,
    )

    op.create_table(
        "registration_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("test_id", sa.Integer(), nullable=False),
        sa.Column(
            "outcome",
            sa.String(length=24),
            nullable=False,
            comment="Separates product defects from controls encountered.",
        ),
        sa.Column("verification_code", sa.String(length=32), nullable=True),
        sa.Column("code_extraction_confidence", sa.Float(), nullable=True),
        sa.Column("account_status", sa.String(length=32), nullable=True),
        sa.Column("steps_json", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["test_id"],
            ["registration_tests.id"],
            name="fk_registration_results_test_id_registration_tests",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_registration_results"),
        sa.UniqueConstraint("test_id", name="uq_registration_results_test_id"),
    )


def _create_job_tables() -> None:
    """Module D: background_jobs, job_logs."""
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "payload_json",
            sa.JSON(),
            nullable=True,
            comment="Redacted before storage: secrets never enter this column.",
        ),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=True,
            comment="Unique: a retried job cannot double-apply its side effects.",
        ),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("worker_name", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_background_jobs"),
        sa.UniqueConstraint("public_id", name="uq_background_jobs_public_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_background_jobs_idempotency_key"),
    )
    op.create_index("ix_background_jobs_public_id", "background_jobs", ["public_id"], unique=True)
    op.create_index("ix_background_jobs_job_type", "background_jobs", ["job_type"], unique=False)
    op.create_index("ix_background_jobs_status", "background_jobs", ["status"], unique=False)
    op.create_index(
        "ix_background_jobs_status_queued", "background_jobs", ["status", "queued_at"], unique=False
    )

    op.create_table(
        "job_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["background_jobs.id"],
            name="fk_job_logs_job_id_background_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_logs"),
    )
    op.create_index("ix_job_logs_job_id", "job_logs", ["job_id"], unique=False)
    op.create_index("ix_job_logs_job_created", "job_logs", ["job_id", "created_at"], unique=False)


def _add_cross_module_links() -> None:
    """Add the two foreign keys that form cycles between module tables.

    Both are added after every table exists, because neither can be declared inline:

    * ``inboxes.campaign_id -> campaigns.id``  (email depends on registration)
    * ``campaigns.job_id    -> background_jobs.id``  (registration depends on jobs)

    Because SQLite cannot add a foreign key to an existing table, each is added with
    ``batch_alter_table``, which rewrites the table via a copy. PostgreSQL uses plain DDL.
    """
    with op.batch_alter_table("inboxes", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_inboxes_campaign_id_campaigns",
            "campaigns",
            ["campaign_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_campaigns_job_id_background_jobs",
            "background_jobs",
            ["job_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Drop every table created by :func:`upgrade`.

    The drops are ordered so a child is always removed before its parent, which keeps the
    operation valid on a database that enforces foreign keys (SQLite does, once the engine
    enables the pragma; PostgreSQL always does).
    """
    # Cross-module links first, by dropping the referencing tables.
    op.drop_table("job_logs")
    op.drop_table("registration_results")
    op.drop_table("registration_tests")
    op.drop_table("messages")
    op.drop_table("background_jobs")
    op.drop_table("campaigns")
    op.drop_table("inboxes")
    op.drop_table("registration_targets")
    op.drop_table("email_domains")
    op.drop_table("proxy_checks")
    op.drop_table("proxies")
    op.drop_table("proxy_sources")
    op.drop_table("audit_logs")
    op.drop_table("users")

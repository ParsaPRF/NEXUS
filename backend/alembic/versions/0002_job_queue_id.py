"""Add the queue correlation column to background_jobs.

Revision ID: 0002_job_queue_id
Revises: 0001_initial
Create Date: 2026-11-15

The database row is the durable record of a job; the queue (in-process or RQ) has its own
identifier. Cancellation needs to map one to the other, so the queue id is stored alongside the
row instead of living only in memory - otherwise a restart would leave jobs that can never be
cancelled.

SQLite cannot add a constraint to an existing table, so the change goes through
``batch_alter_table`` (copy-and-move). PostgreSQL would accept a plain ``ALTER TABLE ADD COLUMN``;
batch mode is valid for both, so one revision covers the two profiles.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0002_job_queue_id"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``background_jobs.queue_job_id``."""
    with op.batch_alter_table("background_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("queue_job_id", sa.String(length=128), nullable=True))
    op.create_index(
        "ix_background_jobs_queue_job_id", "background_jobs", ["queue_job_id"], unique=False
    )


def downgrade() -> None:
    """Remove ``background_jobs.queue_job_id``.

    Dropping the column loses only the mapping to a queue that no longer exists, so no business
    data is destroyed.
    """
    op.drop_index("ix_background_jobs_queue_job_id", table_name="background_jobs")
    with op.batch_alter_table("background_jobs", schema=None) as batch_op:
        batch_op.drop_column("queue_job_id")

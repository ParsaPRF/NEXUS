"""Module D - Task Manager models.

Background jobs are the coordination point for long-running work: batch proxy checks, campaign
execution, inbox cleanup, report generation.

Two properties are worth calling out because they are easy to get wrong:

* **Idempotency.** ``idempotency_key`` is unique. A job that is retried after a crash must not
  run its side effects twice, so the enqueue path claims a key before work starts and a
  duplicate enqueue is rejected rather than silently duplicated.
* **Cooperative cancellation.** ``cancel_requested`` is a flag the worker checks at safe points.
  You cannot safely kill a thread mid-transaction, so cancellation is a request the job honours,
  not a signal that interrupts it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import JobLogLevel, JobStatus


class BackgroundJob(Base, TimestampMixin):
    """One unit of background work and its current state."""

    __tablename__ = "background_jobs"
    __table_args__ = (
        # Job monitor: "what is queued or running, oldest first".
        Index("ix_background_jobs_status_queued", "status", "queued_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    job_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        doc="Registered job type, e.g. proxy.check_batch | campaign.run | inbox.cleanup",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobStatus.QUEUED.value, index=True
    )
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        doc="Job arguments. REDACTED before storage: secrets never enter this column.",
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        unique=True,
        doc="Claimed before execution so a retry cannot double-apply side effects.",
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="Which worker process executed this job."
    )
    queue_job_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
        doc=(
            "Identifier assigned by the queue backend. Needed to cancel a job, and stored here "
            "so a restart does not orphan cancellable work."
        ),
    )

    logs: Mapped[list[JobLog]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="JobLog.created_at",
    )

    @property
    def progress_percent(self) -> float:
        """Progress in ``0..100``. Returns ``0.0`` when the total is not yet known."""
        if self.progress_total <= 0:
            return 0.0
        return min(100.0, round(self.progress_current / self.progress_total * 100, 2))

    @property
    def is_terminal(self) -> bool:
        """True when the job has reached a state it cannot leave."""
        return JobStatus(self.status).is_terminal

    def __repr__(self) -> str:
        return f"<BackgroundJob id={self.id} type={self.job_type!r} status={self.status!r}>"


class JobLog(Base, TimestampMixin):
    """A log line emitted by a running job, streamed to the Task Monitor UI."""

    __tablename__ = "job_logs"
    __table_args__ = (Index("ix_job_logs_job_created", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False, default=JobLogLevel.INFO.value)
    message: Mapped[str] = mapped_column(
        Text, nullable=False, doc="Redacted by the logging filter before persistence."
    )
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    job: Mapped[BackgroundJob] = relationship(back_populates="logs")

    def __repr__(self) -> str:
        return f"<JobLog job_id={self.job_id} level={self.level!r}>"

"""Module C - Registration Lab models.

This module automates sign-up flows in applications the user **owns or is explicitly authorized
to test**. The data model makes that requirement concrete rather than aspirational:

* A target carries ``is_authorized``, defaulting to **false**, plus a mandatory note describing
  who authorized it. A campaign cannot be created for an unauthorized target.
* ``base_url`` passes the SSRF guard and the host allowlist when the target is created, and is
  re-checked before a run.
* There is no proxy column and no proxy relationship anywhere in these tables. Registration
  cannot use the proxy pool because the link does not exist (Decision D8).
* Anti-abuse controls the target applies are recorded as first-class outcomes
  (:class:`~app.models.enums.ResultOutcome`) and stop the campaign. They are never retried
  around.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin
from app.models.enums import CampaignStatus, TargetKind, TestStatus

if TYPE_CHECKING:
    from app.models.email_models import Inbox
    from app.models.job_models import BackgroundJob


class RegistrationTarget(Base, TimestampMixin):
    """An allowlisted application that registration tests may run against."""

    __tablename__ = "registration_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        unique=True,
        doc="Validated by the SSRF guard and host allowlist when written, and again before a run.",
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TargetKind.DEMO.value, doc="demo | api | ui"
    )
    adapter_name: Mapped[str] = mapped_column(
        String(64), nullable=False, doc="Key into the adapter registry."
    )
    is_authorized: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc=(
            "MUST be set to true deliberately, with an authorization note. Defaulting to false "
            "means a forgotten flag fails closed: the campaign is refused."
        ),
    )
    authorization_note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Who authorized testing this target and when. Required for is_authorized=true.",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    campaigns: Mapped[list[Campaign]] = relationship(back_populates="target", passive_deletes=True)

    @property
    def may_be_tested(self) -> bool:
        """True only when the target is active, explicitly authorized, and documented."""
        return self.is_active and self.is_authorized and bool(self.authorization_note)

    def __repr__(self) -> str:
        return (
            f"<RegistrationTarget id={self.id} name={self.name!r} authorized={self.is_authorized}>"
        )


class Campaign(Base, TimestampMixin):
    """A batch of registration tests against one authorized target."""

    __tablename__ = "campaigns"
    # NOTE: no explicit index for `status` here. The column declares `index=True`, which already
    # creates `ix_campaigns_status`; adding a second one in __table_args__ made SQLAlchemy emit
    # the same index name twice and SQLite rejected the CREATE TABLE.

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("registration_targets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CampaignStatus.QUEUED.value, index=True
    )
    total_tests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_tests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_tests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_tests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, doc="Conservative default; raised deliberately."
    )
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=20,
        doc="Fixed request rate. There is no adaptive or escalating mode by design.",
    )
    scenario_config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="Why the campaign stopped, e.g. 'rate limit encountered on test 12'.",
    )

    target: Mapped[RegistrationTarget] = relationship(back_populates="campaigns")
    tests: Mapped[list[RegistrationTest]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    inboxes: Mapped[list[Inbox]] = relationship(back_populates="campaign")
    job: Mapped[BackgroundJob | None] = relationship()

    @property
    def progress_percent(self) -> float:
        """Completion percentage, safe when ``total_tests`` is still zero or unset."""
        total = self.total_tests or 0
        if total <= 0:
            return 0.0
        return min(100.0, round((self.completed_tests or 0) / total * 100, 2))

    def __repr__(self) -> str:
        return f"<Campaign id={self.id} name={self.name!r} status={self.status!r}>"


class RegistrationTest(Base, TimestampMixin):
    """One synthetic identity going through the registration flow.

    A test uses a generated email address and a generated password. It never uses a real
    person's credentials, and the password is held in memory (or in a secret store referenced
    by ``password_ref``) rather than written to this table.
    """

    __tablename__ = "registration_tests"
    __table_args__ = (
        # Re-running a campaign cannot silently create a second test for the same identity.
        UniqueConstraint("campaign_id", "email", name="uq_registration_tests_campaign_email"),
        Index("ix_registration_tests_campaign_status", "campaign_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("inboxes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, doc="Synthetic test identity.")
    password_ref: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        doc="Reference to a generated secret, never the secret itself.",
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=TestStatus.PENDING.value, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="tests")
    result: Mapped[RegistrationResult | None] = relationship(
        back_populates="test",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<RegistrationTest id={self.id} email={self.email!r} status={self.status!r}>"


class RegistrationResult(Base, TimestampMixin):
    """The analysed outcome of one registration test."""

    __tablename__ = "registration_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int] = mapped_column(
        ForeignKey("registration_tests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        doc="One result per test; the unique constraint enforces the 1:1 shape.",
    )
    outcome: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        doc=(
            "Distinguishes product defects (verification_failed, validation_error) from "
            "controls encountered (rate_limited, captcha_encountered, access_denied)."
        ),
    )
    verification_code: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        doc="Captured so a failing verification flow can be diagnosed. This is a code for a "
        "synthetic staging account, not for a real user.",
    )
    code_extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    account_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, doc="Account state as reported by the target adapter."
    )
    steps_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, doc="Ordered step timings - the 'why' behind a failure."
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    test: Mapped[RegistrationTest] = relationship(back_populates="result")

    def __repr__(self) -> str:
        return f"<RegistrationResult test_id={self.test_id} outcome={self.outcome!r}>"

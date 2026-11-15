"""Campaign execution: the registration runner.

Guarantees, in order of importance:

1. **It stops when the target refuses.** A rate limit, a CAPTCHA or an access denial ends the
   campaign with status ``stopped_by_target``. There is no retry, no identity change, no proxy
   rotation and no delay-and-resume: those would be attempts to defeat a control.
2. **Rate and concurrency are fixed.** ``rate_limit_per_minute`` spaces submissions; ``concurrency``
   bounds how many run at once. Neither adapts to the target's behaviour.
3. **Every test is recorded**, including why it failed, and a cancelled campaign stops at the next
   identity rather than mid-transaction.
4. **Database access is serialized.** One session is shared by the worker coroutines, so every
   write happens under a lock; network work happens outside it.

The runner never reads a human mailbox and never submits a guessed code: only codes extracted with
confidence ``>= 0.7`` are sent to the target.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.enums import CampaignStatus, ResultOutcome, TestStatus
from app.models.registration_models import (
    Campaign,
    RegistrationResult,
    RegistrationTarget,
    RegistrationTest,
)
from app.modules.email_manager.service import InboxService
from app.modules.registration_lab.adapters import (
    AccountState,
    AdapterResponse,
    RegistrationAdapter,
    build_adapter,
)
from app.modules.registration_lab.analyzer import Analysis, analyze
from app.modules.registration_lab.authorization import ensure_target_usable
from app.modules.registration_lab.repository import RegistrationRepository
from app.modules.registration_lab.verification import InboxEmailVerifier, VerificationOutcome

logger = get_logger(__name__)

#: Password length for synthetic identities. Generated, never reused, never stored.
_SYNTHETIC_PASSWORD_BYTES = 12


class _RateLimiter:
    """Spaces operations evenly: one every ``60 / per_minute`` seconds.

    Fixed spacing, not a token bucket that can burst: burstiness is exactly what makes automation
    look abusive.
    """

    def __init__(self, per_minute: int) -> None:
        self._interval = 60.0 / max(1, per_minute)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        """Wait until the next slot is due."""
        async with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait_for > 0:
            await asyncio.sleep(wait_for)


class CampaignExecutor:
    """Runs one campaign against one authorized target."""

    def __init__(
        self,
        session: Any,
        settings: Settings,
        campaign: Campaign,
        target: RegistrationTarget,
        *,
        adapter: RegistrationAdapter | None = None,
        inbox_service: InboxService | None = None,
        email_timeout_seconds: float = 30.0,
    ) -> None:
        self._session = session
        self._settings = settings
        self._campaign = campaign
        self._target = target
        self._adapter = adapter or build_adapter(target, settings)
        self._inboxes = inbox_service or InboxService(session, settings)
        self._verifier = InboxEmailVerifier(self._inboxes)
        self._email_timeout = email_timeout_seconds
        self._repo = RegistrationRepository(session)
        self._db_lock = asyncio.Lock()
        self._stop = False
        self._stop_reason: str | None = None
        self._rate = _RateLimiter(campaign.rate_limit_per_minute)

    # ------------------------------------------------------------------ entry point

    async def run(self, context: Any | None = None) -> dict[str, Any]:
        """Execute every pending test, then finalize the campaign.

        Returns:
            A summary dict for the job log.
        """
        # Re-checked at run time, not only at creation: a target can be de-authorized or
        # deactivated after the campaign was queued.
        ensure_target_usable(self._target, self._settings)

        await self._mark_running()
        tests = await self._repo.pending_tests(self._campaign.id)
        semaphore = asyncio.Semaphore(self._campaign.concurrency)

        await asyncio.gather(*(self._run_one(test, semaphore, context) for test in tests))
        return await self._finalize()

    # ------------------------------------------------------------------ one test

    async def _run_one(
        self, test: RegistrationTest, semaphore: asyncio.Semaphore, context: Any | None
    ) -> None:
        """Run one identity through the flow, then record the verdict."""
        async with semaphore:
            if self._stop or self._campaign.status == CampaignStatus.CANCELLED.value:
                return
            if context is not None:
                # Cooperative cancellation: safe to stop here, nothing is half-written.
                context.check_cancelled()

            await self._rate.acquire()
            started = time.perf_counter()
            outcome = await self._attempt(test)
            duration_ms = int((time.perf_counter() - started) * 1000)

            await self._persist(test, outcome, duration_ms)
            if context is not None:
                context.advance(message=f"{test.email}: {outcome.outcome.value}")
            if outcome.stop_campaign:
                self._stop = True
                self._stop_reason = outcome.detail

    async def _attempt(self, test: RegistrationTest) -> Analysis:
        """Submit, wait for the code, verify, and query the account."""
        password = secrets.token_urlsafe(_SYNTHETIC_PASSWORD_BYTES)

        submit: AdapterResponse = await self._adapter.submit(email=test.email, password=password)
        await self._set_test_status(test, TestStatus.SUBMITTED)
        if not submit.ok:
            return analyze(submit=submit)

        await self._set_test_status(test, TestStatus.AWAITING_EMAIL)
        verification: VerificationOutcome = await self._verifier.wait_for_code(
            inbox_public_id=await self._inbox_public_id(test),
            timeout_seconds=self._email_timeout,
        )
        if not verification.trustworthy:
            return analyze(
                submit=submit,
                code_found=verification.found,
                timed_out=not verification.found,
            )

        verify = await self._adapter.verify(email=test.email, code=verification.code or "")
        account: AccountState | None = None
        if verify.ok:
            account = await self._adapter.account_status(email=test.email)

        analysis = analyze(
            submit=submit,
            verify=verify,
            account=account,
            code_found=True,
        )
        analysis.steps["verification_code"] = verification.code
        analysis.steps["verification_confidence"] = verification.confidence
        analysis.steps["verification_polls"] = verification.polls
        analysis.steps["verification_waited_ms"] = verification.waited_ms
        return analysis

    async def _inbox_public_id(self, test: RegistrationTest) -> str:
        """Return the public id of the inbox attached to ``test``."""
        inbox = await self._inboxes.get_inbox_by_id(test.inbox_id)
        return inbox.public_id

    # ------------------------------------------------------------------ persistence

    async def _mark_running(self) -> None:
        async with self._db_lock:
            self._campaign.status = CampaignStatus.RUNNING.value
            self._campaign.started_at = datetime.now(UTC)
            await self._session.commit()

    async def _set_test_status(self, test: RegistrationTest, status: TestStatus) -> None:
        async with self._db_lock:
            test.status = status.value
            if status is TestStatus.SUBMITTED and test.started_at is None:
                test.started_at = datetime.now(UTC)
            await self._session.flush()

    async def _persist(self, test: RegistrationTest, analysis: Analysis, duration_ms: int) -> None:
        """Record the result and update the test and campaign counters."""
        async with self._db_lock:
            now = datetime.now(UTC)
            test.finished_at = now
            test.status = _test_status_for(analysis)
            if not analysis.succeeded:
                test.error_code = analysis.outcome.value
                test.error_message = analysis.detail[:512]

            self._session.add(
                RegistrationResult(
                    test_id=test.id,
                    outcome=analysis.outcome.value,
                    verification_code=analysis.steps.get("verification_code"),  # type: ignore[arg-type]
                    code_extraction_confidence=analysis.steps.get("verification_confidence"),  # type: ignore[arg-type]
                    account_status=analysis.steps.get("account_status"),  # type: ignore[arg-type]
                    steps_json=analysis.steps,
                    duration_ms=duration_ms,
                )
            )
            self._campaign.completed_tests = (self._campaign.completed_tests or 0) + 1
            if analysis.succeeded:
                self._campaign.succeeded_tests = (self._campaign.succeeded_tests or 0) + 1
            else:
                self._campaign.failed_tests = (self._campaign.failed_tests or 0) + 1
            await self._session.commit()

    async def _finalize(self) -> dict[str, Any]:
        """Set the campaign's terminal status and return a summary."""
        async with self._db_lock:
            self._campaign.finished_at = datetime.now(UTC)
            if self._campaign.status == CampaignStatus.CANCELLED.value:
                pass  # operator cancellation wins over a normal completion
            elif self._stop:
                self._campaign.status = CampaignStatus.STOPPED_BY_TARGET.value
                self._campaign.stop_reason = (self._stop_reason or "target control encountered")[
                    :255
                ]
            else:
                self._campaign.status = CampaignStatus.COMPLETED.value
            await self._session.commit()

            summary = {
                "campaign_id": self._campaign.id,
                "status": self._campaign.status,
                "completed": self._campaign.completed_tests,
                "succeeded": self._campaign.succeeded_tests,
                "failed": self._campaign.failed_tests,
                "stopped": self._stop,
            }
        logger.info(
            "Campaign %s finished: %s",
            self._campaign.public_id,
            summary,
            extra={"module_name": "registration_lab"},
        )
        return summary


def _test_status_for(analysis: Analysis) -> str:
    """Map an analysis to the test's terminal status."""
    if analysis.succeeded:
        return TestStatus.ACTIVE.value
    if analysis.outcome.is_control_encountered:
        return TestStatus.BLOCKED_BY_TARGET.value
    return TestStatus.FAILED.value


__all__ = ["CampaignExecutor", "ResultOutcome"]

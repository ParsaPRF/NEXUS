"""Email-side verification: obtaining the code for an identity.

This is the ``EmailVerificationAdapter`` role from the brief, implemented over NEXUS's own inbox
service. It is deliberately separated from the target adapter: the mailbox belongs to NEXUS, the
target does not get to see it.

The loop is bounded in **both** directions:

* it stops after ``timeout_seconds`` (the email may never arrive);
* it stops after ``max_polls`` (so a backend that keeps returning malformed messages cannot spin
  forever).

Confidence matters: a low-confidence extraction is reported as such, and the caller can refuse to
submit a guess rather than sending a random number to the target.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Default spacing between polls. Short enough for responsive tests, long enough not to hammer
#: the mail backend.
DEFAULT_POLL_INTERVAL = 0.25

#: Confidence at or above which an extracted code is submitted to the target.
SUBMIT_CONFIDENCE = 0.7


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """What the mailbox yielded for one identity."""

    found: bool
    code: str | None = None
    confidence: float = 0.0
    waited_ms: int | None = None
    polls: int = 0
    message: str | None = None

    @property
    def trustworthy(self) -> bool:
        """True when a code was found with enough confidence to submit."""
        return self.found and self.code is not None and self.confidence >= SUBMIT_CONFIDENCE


class InboxEmailVerifier:
    """Waits for a verification code to arrive in one inbox."""

    def __init__(self, inbox_service) -> None:  # type: ignore[no-untyped-def]
        self._inboxes = inbox_service

    async def wait_for_code(
        self,
        *,
        inbox_public_id: str,
        timeout_seconds: float = 30.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_polls: int = 120,
    ) -> VerificationOutcome:
        """Poll the inbox until a code appears or the budget runs out.

        Args:
            inbox_public_id: Public id of the inbox that belongs to this identity.
            timeout_seconds: Overall deadline.
            poll_interval: Delay between polls.
            max_polls: Hard cap on polls, independent of the deadline.

        Returns:
            A :class:`VerificationOutcome`. ``found`` is false on timeout - that is a normal,
            reportable result, not an exception.
        """
        started = time.perf_counter()
        deadline = started + timeout_seconds
        polls = 0

        while polls < max_polls and time.perf_counter() < deadline:
            polls += 1
            try:
                await self._inboxes.poll_inbox(inbox_public_id)
                messages, _total = await self._inboxes.list_messages(inbox_public_id, limit=20)
            except Exception as exc:  # noqa: BLE001 - a broken poll must not abort the campaign
                logger.warning(
                    "Polling %s failed: %s",
                    inbox_public_id,
                    type(exc).__name__,
                    extra={"module_name": "registration_lab"},
                )
                messages = []

            best = _best_code(messages)
            if best is not None:
                return VerificationOutcome(
                    found=True,
                    code=best["code"],
                    confidence=float(best["confidence"] or 0.0),
                    waited_ms=int((time.perf_counter() - started) * 1000),
                    polls=polls,
                )

            await asyncio.sleep(poll_interval)

        return VerificationOutcome(
            found=False,
            waited_ms=int((time.perf_counter() - started) * 1000),
            polls=polls,
            message="No verification email arrived before the deadline.",
        )


def _best_code(messages: list) -> dict[str, object] | None:  # type: ignore[type-arg]
    """Return the highest-confidence extracted code among ``messages``, if any."""
    candidates = [message for message in messages if getattr(message, "extracted_code", None)]
    if not candidates:
        return None
    best = max(candidates, key=lambda message: message.extraction_confidence or 0.0)
    return {
        "code": best.extracted_code,
        "confidence": best.extraction_confidence,
        "message_id": best.id,
    }

"""Result analysis: turning adapter outcomes into a reportable verdict.

This module is the single place where "what happened" becomes "what we record". Separating it from
the executor means the classification rules can be tested exhaustively without any network.

The most important rule it encodes: **a product defect and a control encountered are different
things.** A failed verification is a bug report about the tested application. A 429 or a CAPTCHA
is a boundary NEXUS respects, and it stops the campaign. Averaging them into one failure rate would
hide exactly the facts a QA report exists to surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.enums import ResultOutcome
from app.modules.registration_lab.adapters import (
    AccountState,
    AdapterOutcome,
    AdapterResponse,
)

#: Control outcome -> recorded result outcome.
_CONTROL_MAP: dict[AdapterOutcome, ResultOutcome] = {
    AdapterOutcome.RATE_LIMITED: ResultOutcome.RATE_LIMITED,
    AdapterOutcome.CAPTCHA: ResultOutcome.CAPTCHA_ENCOUNTERED,
    AdapterOutcome.ACCESS_DENIED: ResultOutcome.ACCESS_DENIED,
}


@dataclass(frozen=True, slots=True)
class Analysis:
    """The verdict for one registration test."""

    outcome: ResultOutcome
    stop_campaign: bool
    detail: str
    steps: dict[str, object] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """True only for a fully verified, active account."""
        return self.outcome is ResultOutcome.SUCCESS


def analyze(
    *,
    submit: AdapterResponse,
    verify: AdapterResponse | None = None,
    account: AccountState | None = None,
    code_found: bool = False,
    timed_out: bool = False,
) -> Analysis:
    """Classify one registration attempt.

    Args:
        submit: The result of the registration request.
        verify: The result of the verification request, if it was reached.
        account: The account state afterwards, if it was queried.
        code_found: Whether a usable verification code arrived.
        timed_out: Whether the mailbox deadline elapsed.

    Returns:
        An :class:`Analysis` with the recorded outcome and whether the campaign must stop.
    """
    steps: dict[str, object] = {
        "submit": submit.outcome.value,
        "submit_http_status": submit.http_status,
        "submit_elapsed_ms": submit.elapsed_ms,
        "code_found": code_found,
    }

    # --- the submission itself was refused or broken -------------------------------
    if submit.outcome.is_control:
        return Analysis(
            outcome=_CONTROL_MAP[submit.outcome],
            stop_campaign=True,
            detail=(
                f"The target refused the registration during submission ({submit.outcome.value})."
            ),
            steps=steps,
        )
    if submit.outcome is AdapterOutcome.DUPLICATE:
        return Analysis(
            outcome=ResultOutcome.VALIDATION_ERROR,
            stop_campaign=False,
            detail="The target already has an account for this address.",
            steps=steps,
        )
    if submit.outcome is AdapterOutcome.REJECTED:
        return Analysis(
            outcome=ResultOutcome.VALIDATION_ERROR,
            stop_campaign=False,
            detail=f"The target rejected the registration payload: {submit.message or 'no detail'}",
            steps=steps,
        )
    if submit.outcome is AdapterOutcome.ERROR:
        return Analysis(
            outcome=ResultOutcome.UNEXPECTED_ERROR,
            stop_campaign=False,
            detail=f"The registration request failed: {submit.message or 'no detail'}",
            steps=steps,
        )

    # --- accepted; the email step ---------------------------------------------------
    if timed_out or not code_found:
        return Analysis(
            outcome=ResultOutcome.EMAIL_TIMEOUT,
            stop_campaign=False,
            detail="Registration was accepted but no verification code arrived in time.",
            steps=steps,
        )

    if verify is None:
        return Analysis(
            outcome=ResultOutcome.UNEXPECTED_ERROR,
            stop_campaign=False,
            detail="Verification was never attempted.",
            steps=steps,
        )

    steps["verify"] = verify.outcome.value
    steps["verify_http_status"] = verify.http_status
    steps["verify_elapsed_ms"] = verify.elapsed_ms

    if verify.outcome.is_control:
        return Analysis(
            outcome=_CONTROL_MAP[verify.outcome],
            stop_campaign=True,
            detail=f"The target refused verification ({verify.outcome.value}).",
            steps=steps,
        )
    if verify.outcome is AdapterOutcome.ERROR:
        return Analysis(
            outcome=ResultOutcome.UNEXPECTED_ERROR,
            stop_campaign=False,
            detail=f"The verification request failed: {verify.message or 'no detail'}",
            steps=steps,
        )
    if verify.outcome is not AdapterOutcome.ACCEPTED:
        return Analysis(
            outcome=ResultOutcome.VERIFICATION_FAILED,
            stop_campaign=False,
            detail=(
                "The target rejected the verification code"
                + (f": {verify.message}" if verify.message else ".")
            ),
            steps=steps,
        )

    # --- verification accepted; is the account actually usable? ----------------------
    if account is not None:
        steps["account_status"] = account.status
        steps["account_exists"] = account.exists
    if account is None or not account.is_active:
        return Analysis(
            outcome=ResultOutcome.VERIFICATION_FAILED,
            stop_campaign=False,
            detail=(
                "The target accepted the code but the account is not active "
                f"(status={getattr(account, 'status', None)!r})."
            ),
            steps=steps,
        )

    return Analysis(
        outcome=ResultOutcome.SUCCESS,
        stop_campaign=False,
        detail="Registration completed and the account is active.",
        steps=steps,
    )

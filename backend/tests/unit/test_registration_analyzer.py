"""Result-analysis tests.

No network, no database: the classification rules are pure, so every combination can be pinned.
These tests are the contract for the most important distinction in the module - a product defect
versus a control the tool respected - and for when the campaign must stop.
"""

from __future__ import annotations

import pytest

from app.models.enums import ResultOutcome
from app.modules.registration_lab.adapters import (
    AccountState,
    AdapterOutcome,
    AdapterResponse,
)
from app.modules.registration_lab.analyzer import analyze


def _response(outcome: AdapterOutcome, status: int = 200) -> AdapterResponse:
    return AdapterResponse(outcome=outcome, http_status=status, message=None, elapsed_ms=5)


ACTIVE = AccountState(exists=True, status="active")
PENDING = AccountState(exists=True, status="pending_verification")


class TestSuccess:
    """The only path that counts as success."""

    def test_fully_verified_active_account(self) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202),
            verify=_response(AdapterOutcome.ACCEPTED),
            account=ACTIVE,
            code_found=True,
        )
        assert analysis.outcome is ResultOutcome.SUCCESS
        assert analysis.succeeded is True
        assert analysis.stop_campaign is False

    def test_steps_are_recorded(self) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202),
            verify=_response(AdapterOutcome.ACCEPTED),
            account=ACTIVE,
            code_found=True,
        )
        assert analysis.steps["submit"] == "accepted"
        assert analysis.steps["account_status"] == "active"


class TestProductDefects:
    """Failures that are bug reports about the tested application."""

    def test_no_email_arrived(self) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202), code_found=False, timed_out=True
        )
        assert analysis.outcome is ResultOutcome.EMAIL_TIMEOUT
        assert analysis.stop_campaign is False

    def test_code_arrived_but_verification_rejected(self) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202),
            verify=_response(AdapterOutcome.REJECTED, 400),
            account=PENDING,
            code_found=True,
        )
        assert analysis.outcome is ResultOutcome.VERIFICATION_FAILED

    def test_verified_but_account_still_pending(self) -> None:
        # The target accepted the code but never activated the account: a real defect worth
        # reporting, not a success.
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202),
            verify=_response(AdapterOutcome.ACCEPTED),
            account=PENDING,
            code_found=True,
        )
        assert analysis.outcome is ResultOutcome.VERIFICATION_FAILED

    def test_account_missing_after_verification(self) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202),
            verify=_response(AdapterOutcome.ACCEPTED),
            account=AccountState(exists=False, status=None),
            code_found=True,
        )
        assert analysis.outcome is ResultOutcome.VERIFICATION_FAILED

    def test_duplicate_identity(self) -> None:
        analysis = analyze(submit=_response(AdapterOutcome.DUPLICATE, 409))
        assert analysis.outcome is ResultOutcome.VALIDATION_ERROR

    def test_rejected_payload(self) -> None:
        analysis = analyze(submit=_response(AdapterOutcome.REJECTED, 422))
        assert analysis.outcome is ResultOutcome.VALIDATION_ERROR

    def test_transport_error_on_submit(self) -> None:
        analysis = analyze(submit=_response(AdapterOutcome.ERROR))
        assert analysis.outcome is ResultOutcome.UNEXPECTED_ERROR

    def test_transport_error_on_verify(self) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202),
            verify=_response(AdapterOutcome.ERROR),
            code_found=True,
        )
        assert analysis.outcome is ResultOutcome.UNEXPECTED_ERROR

    def test_verification_never_attempted(self) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202), verify=None, code_found=True
        )
        assert analysis.outcome is ResultOutcome.UNEXPECTED_ERROR


class TestControls:
    """Controls stop the campaign and are recorded as their own outcomes."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (AdapterOutcome.RATE_LIMITED, ResultOutcome.RATE_LIMITED),
            (AdapterOutcome.CAPTCHA, ResultOutcome.CAPTCHA_ENCOUNTERED),
            (AdapterOutcome.ACCESS_DENIED, ResultOutcome.ACCESS_DENIED),
        ],
    )
    def test_control_during_submission_stops(
        self, outcome: AdapterOutcome, expected: ResultOutcome
    ) -> None:
        analysis = analyze(submit=_response(outcome, 429))
        assert analysis.outcome is expected
        assert analysis.stop_campaign is True
        assert analysis.succeeded is False

    @pytest.mark.parametrize(
        "outcome",
        [AdapterOutcome.RATE_LIMITED, AdapterOutcome.CAPTCHA, AdapterOutcome.ACCESS_DENIED],
    )
    def test_control_during_verification_stops(self, outcome: AdapterOutcome) -> None:
        analysis = analyze(
            submit=_response(AdapterOutcome.ACCEPTED, 202),
            verify=_response(outcome, 403),
            code_found=True,
        )
        assert analysis.stop_campaign is True
        assert analysis.outcome.is_control_encountered

    def test_controls_are_not_counted_as_defects(self) -> None:
        # The enum property is what the summary uses to keep the two apart.
        analysis = analyze(submit=_response(AdapterOutcome.RATE_LIMITED, 429))
        assert analysis.outcome.is_control_encountered
        assert analysis.outcome is not ResultOutcome.VALIDATION_ERROR

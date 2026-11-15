"""Identity parsing tests.

Imported identities are a trust boundary: they decide which addresses a campaign will register.
The rules are narrow on purpose - an address we cannot read mail for produces a misleading
"email timeout" later, so it is refused here with an explanation instead.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ValidationError
from app.modules.registration_lab.identities import (
    MAX_IDENTITIES_PER_CAMPAIGN,
    domain_of,
    local_part_of,
    parse_identity_text,
)


class TestParsing:
    """Accepted input shapes."""

    def test_plain_list(self) -> None:
        assert parse_identity_text("a@example.test\nb@example.test") == [
            "a@example.test",
            "b@example.test",
        ]

    def test_csv_with_email_header(self) -> None:
        assert parse_identity_text("email\nqa-1@example.test\n") == ["qa-1@example.test"]

    def test_csv_with_address_header(self) -> None:
        assert parse_identity_text("address\nqa-1@example.test\n") == ["qa-1@example.test"]

    def test_header_is_skipped(self) -> None:
        content = "email,label\nqa-1@example.test,first\n"
        assert parse_identity_text(content) == ["qa-1@example.test"]

    def test_addresses_are_lowercased(self) -> None:
        assert parse_identity_text("QA-1@Example.TEST") == ["qa-1@example.test"]

    def test_duplicates_are_removed(self) -> None:
        assert parse_identity_text("a@example.test\na@example.test") == ["a@example.test"]

    def test_blank_lines_and_comments_are_ignored(self) -> None:
        content = "\n# a comment\na@example.test\n\n"
        assert parse_identity_text(content) == ["a@example.test"]

    def test_whitespace_is_trimmed(self) -> None:
        assert parse_identity_text("  a@example.test  ") == ["a@example.test"]

    def test_limit_is_enforced(self) -> None:
        content = "\n".join(f"qa-{index}@example.test" for index in range(5))
        with pytest.raises(ValidationError, match="at most"):
            parse_identity_text(content, limit=3)


class TestRejections:
    """Input that must be refused, with a reason."""

    def test_empty_input(self) -> None:
        with pytest.raises(ValidationError):
            parse_identity_text("")

    def test_only_whitespace(self) -> None:
        with pytest.raises(ValidationError):
            parse_identity_text("   \n  ")

    def test_invalid_address_reports_the_line(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            parse_identity_text("a@example.test\nnot-an-email\n")
        problems = excinfo.value.details["problems"]
        assert problems[0]["line_number"] == 2

    def test_address_without_domain_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse_identity_text("nodomain@")

    def test_limit_constant_is_reasonable(self) -> None:
        assert 1 <= MAX_IDENTITIES_PER_CAMPAIGN <= 1000


class TestHelpers:
    """Small string helpers."""

    def test_domain_of(self) -> None:
        assert domain_of("qa-1@Example.TEST") == "example.test"

    def test_local_part_of(self) -> None:
        assert local_part_of("QA-1@example.test") == "qa-1"

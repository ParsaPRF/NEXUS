"""Verification-code extraction tests.

A corpus rather than a couple of examples: extraction is the piece that silently breaks a
registration test, and the failure mode ("verification failed" with no clue) is expensive to
debug. Every case here is a shape real verification emails actually use, or a shape that must
*not* be mistaken for a code.
"""

from __future__ import annotations

import pytest

from app.modules.email_manager.extraction import (
    MAX_CODE_LENGTH,
    MIN_CODE_LENGTH,
    codes_in_text,
    extract_verification_code,
    html_to_text,
)


class TestLabelledCodes:
    """The strongest signal: a code next to a word that means "code"."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Your verification code is 481920", "481920"),
            ("Your verification code: 481920", "481920"),
            ("Verification code - 481920", "481920"),
            ("Your OTP is 739214", "739214"),
            ("Your PIN is 2468", "2468"),
            ("Use this passcode: 90210", "90210"),
            ("Security code: 556677", "556677"),
            ("Your confirmation code is 334455.", "334455"),
            ('Your login code is "776655"', "776655"),
            ("Your one-time code is 1234", "1234"),
        ],
    )
    def test_extracts_the_code(self, text: str, expected: str) -> None:
        result = extract_verification_code(text=text)
        assert result.code == expected
        assert result.confidence >= 0.7
        assert result.strategy is not None

    def test_labelled_confidence_is_high(self) -> None:
        result = extract_verification_code(text="Your verification code is 481920")
        assert result.confidence == pytest.approx(0.9)

    def test_separated_digits_are_joined(self) -> None:
        # Many senders format a code as 123-456 or 123 456 for readability.
        result = extract_verification_code(text="Your security code is 123-456")
        assert result.code == "123456"
        assert result.confidence >= 0.7

    def test_words_between_label_and_code_are_tolerated(self) -> None:
        result = extract_verification_code(text="Please use the following code to continue: 654321")
        assert result.code == "654321"

    def test_alphanumeric_code_after_a_label(self) -> None:
        result = extract_verification_code(text="Your access code is AB12CD")
        assert result.code == "AB12CD"


class TestWeakSignals:
    """Standalone numbers are a fallback, with lower confidence."""

    def test_standalone_six_digits_are_found_with_low_confidence(self) -> None:
        result = extract_verification_code(text="Ticket 888123")
        assert result.code == "888123"
        assert result.confidence < 0.7

    def test_min_confidence_filters_weak_candidates(self) -> None:
        result = extract_verification_code(text="Ticket 888123", min_confidence=0.8)
        assert result.code is None
        assert result.candidates  # the candidate is still reported for debugging

    def test_digits_near_a_label_beat_a_bare_number(self) -> None:
        result = extract_verification_code(
            text="Reference 998877. Use your code 445566 to continue."
        )
        assert result.code == "445566"
        assert result.confidence > 0.5


class TestRejections:
    """Things that look like codes but must not be returned."""

    @pytest.mark.parametrize(
        "text",
        [
            "Your order has shipped.",
            "",
            "   ",
            "Your order 12345 has shipped.",  # 5 digits, no label: too weak
            "Invoice 2026 was paid.",  # year-like
            "Call us on 12345678901.",  # inside a longer number
            "Tracking: 000000",  # placeholder-looking standalone
        ],
    )
    def test_no_code_found(self, text: str) -> None:
        result = extract_verification_code(text=text)
        assert result.code is None
        assert result.found is False

    def test_year_is_not_confused_with_a_code(self) -> None:
        result = extract_verification_code(
            text="Your 2026 statement is ready. Your code is 771122."
        )
        assert result.code == "771122"

    def test_no_bodies_means_no_code(self) -> None:
        assert extract_verification_code().code is None
        assert extract_verification_code(text=None, html=None).code is None


class TestHtmlHandling:
    """HTML is parsed for text, never rendered."""

    def test_extracts_from_html_only_message(self) -> None:
        result = extract_verification_code(
            html="<html><body><p>Your code is <b>481920</b></p></body></html>"
        )
        assert result.code == "481920"

    def test_prefers_plain_text_when_both_exist(self) -> None:
        result = extract_verification_code(
            text="Your code is 111222",
            html="<p>Your code is <b>999888</b></p>",
        )
        assert result.code == "111222"

    def test_html_to_text_strips_tags(self) -> None:
        assert html_to_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_html_to_text_drops_script_content(self) -> None:
        # Script content is not visible text and must never be treated as a code source.
        assert html_to_text("<p>Hello</p><script>var x = 481920;</script>") == "Hello"

    def test_html_to_text_drops_style_content(self) -> None:
        assert "font-size" not in html_to_text("<style>p { font-size: 12px }</style><p>Hi</p>")

    def test_malformed_html_does_not_raise(self) -> None:
        assert html_to_text("<p>unclosed <b>bold") in {"unclosed bold", "unclosed bold "}

    def test_empty_html(self) -> None:
        assert html_to_text("") == ""

    def test_entities_are_decoded(self) -> None:
        assert "&" in html_to_text("<p>Tom &amp; Jerry</p>")


class TestCandidates:
    """The ranked candidate list is what a human debugging a failure needs."""

    def test_candidates_are_ranked_by_confidence(self) -> None:
        result = extract_verification_code(text="Your code is 445566. Reference 998877.")
        confidences = [candidate.confidence for candidate in result.candidates]
        assert confidences == sorted(confidences, reverse=True)

    def test_best_candidate_matches_the_result(self) -> None:
        result = extract_verification_code(text="Your code is 445566. Reference 998877.")
        assert result.code == result.candidates[0].code

    def test_candidate_carries_its_strategy(self) -> None:
        result = extract_verification_code(text="Your code is 445566")
        assert "labelled" in result.candidates[0].strategy


class TestHelpers:
    """Small public helpers used by debugging tools."""

    def test_codes_in_text(self) -> None:
        assert codes_in_text("Your code is 481920") == ["481920"]

    def test_codes_in_text_returns_empty_when_none(self) -> None:
        assert codes_in_text("nothing here") == []

    @pytest.mark.parametrize(("minimum", "maximum"), [(MIN_CODE_LENGTH, MAX_CODE_LENGTH)])
    def test_length_bounds_are_sane(self, minimum: int, maximum: int) -> None:
        assert 3 <= minimum <= maximum <= 10

    def test_is_confident_helper(self) -> None:
        assert extract_verification_code(text="code is 481920").is_confident()
        assert not extract_verification_code(text="Ticket 888123").is_confident()

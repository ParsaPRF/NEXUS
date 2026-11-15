"""Verification-code extraction from test emails.

This is a **pure function of the message text**: no database, no network, no clock. That makes
it exhaustively testable, which matters because a wrong extraction silently breaks a registration
test in a way that is hard to diagnose ("verification failed" with no clue why).

Two design rules:

1. **Never return a code you are not reasonably sure about.** A false positive sends garbage to
   the target application and produces a misleading failure. Confidence is reported alongside the
   value so callers can refuse a weak guess.
2. **HTML is converted to text by parsing, not by rendering.** ``body_html`` is untrusted input;
   we extract its text with :class:`html.parser.HTMLParser`, which never executes anything and
   never resolves an external reference (threat T6/T10).

Extraction strategies, strongest first:

* **labelled** - a code-like token within a short distance of a word such as "code", "OTP",
  "PIN", "passcode" (``"Your verification code is 481920"``) - confidence 0.9
* **separated digits** - a ``123-456`` / ``123 456`` group next to a label - confidence 0.8
* **near-label digits** - digits inside the same sentence as a label - confidence 0.65
* **standalone six digits** - a bare 6-digit group, with obvious non-codes excluded -
  confidence 0.45
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

#: Shortest and longest verification code we will accept.
MIN_CODE_LENGTH = 4
MAX_CODE_LENGTH = 8

#: Words that introduce a verification code. Kept deliberately narrow: matching "number" or
#: "reference" would pull in order ids and invoice numbers.
LABEL_WORDS: tuple[str, ...] = (
    "verification code",
    "confirmation code",
    "security code",
    "access code",
    "login code",
    "one-time code",
    "one time code",
    "one-time password",
    "one time password",
    "passcode",
    "verification",
    "confirm",
    "otp",
    "pin",
    "code",
)

#: Maximum number of characters between the label and the code.
_LABEL_WINDOW = 40

#: Values that look like codes but almost certainly are not, in this context.
_NON_CODE_VALUES: frozenset[str] = frozenset(
    {
        "000000",
        "111111",
        "123456",  # overwhelmingly a placeholder in documentation, but see the note below
    }
)


class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, ignoring script and style content."""

    _SKIP_TAGS = frozenset({"script", "style", "head", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    @property
    def text(self) -> str:
        """Accumulated visible text, with whitespace collapsed."""
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def html_to_text(html: str) -> str:
    """Return the visible text of ``html``.

    Parsing (rather than rendering) is what makes this safe: no CSS, no images, no external
    fetches, and script/style content is dropped.
    """
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must never break extraction
        return ""
    return parser.text


@dataclass(frozen=True, slots=True)
class CodeCandidate:
    """One possible verification code, with how it was found."""

    code: str
    confidence: float
    strategy: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The outcome of extraction.

    ``code`` is ``None`` when nothing plausible was found. ``candidates`` is ordered best-first,
    which is what a human debugging a failing test wants to see.
    """

    code: str | None
    confidence: float
    strategy: str | None
    candidates: tuple[CodeCandidate, ...] = ()

    @property
    def found(self) -> bool:
        """True when a code was extracted."""
        return self.code is not None

    def is_confident(self, threshold: float = 0.7) -> bool:
        """True when the best candidate clears ``threshold``."""
        return self.code is not None and self.confidence >= threshold


def _label_pattern() -> re.Pattern[str]:
    """Build the "label followed by a code" pattern."""
    labels = "|".join(re.escape(word) for word in sorted(LABEL_WORDS, key=len, reverse=True))
    # The gap may contain ordinary words ("code **is** 481920", "code **to continue:**"),
    # but it stops at a sentence boundary so a label in one sentence cannot capture a number
    # from the next one. The lookahead requires the captured token to contain a digit, so prose
    # such as "code: hello world 481920" cannot match the word "hello" as if it were a code.
    return re.compile(
        rf"(?i)\b(?:{labels})\b[^.!?\n]{{0,{_LABEL_WINDOW}}}?"
        rf"(?=[A-Z0-9]*\d)([A-Z0-9]{{{MIN_CODE_LENGTH},{MAX_CODE_LENGTH}}})\b"
    )


_LABELED = _label_pattern()
_SEPARATED_DIGITS = re.compile(
    r"(?i)\b(?:code|otp|pin|passcode)\b[^0-9]{0,20}(\d{3})[-\s](\d{3})\b"
)
_NEAR_LABEL_DIGITS = re.compile(
    rf"(?i)\b(?:code|otp|pin|passcode|verification)\b[^0-9]{{0,{_LABEL_WINDOW}}}?(\d{{{MIN_CODE_LENGTH},{MAX_CODE_LENGTH}}})\b"
)
_STANDALONE_DIGITS = re.compile(rf"\b(\d{{{MIN_CODE_LENGTH},{MAX_CODE_LENGTH}}})\b")
_YEAR_LIKE = re.compile(r"^(19|20)\d{2}$")


def _normalize(token: str) -> str:
    """Strip separators and uppercase a candidate token."""
    return re.sub(r"[^A-Za-z0-9]", "", token).upper()


def _is_plausible(token: str, *, standalone: bool) -> bool:
    """Apply the cheap sanity checks that remove the obvious non-codes.

    A standalone group must be at least 6 digits: 4- and 5-digit standalone numbers are far more
    likely to be quantities or years than verification codes. Labelled tokens are trusted more,
    because the label is the evidence.
    """
    if not MIN_CODE_LENGTH <= len(token) <= MAX_CODE_LENGTH:
        return False
    if not any(character.isdigit() for character in token):
        return False
    if _YEAR_LIKE.match(token):
        return False
    if standalone:
        if not token.isdigit() or len(token) < 6:  # noqa: PLR2004 - documented threshold
            return False
        if token in _NON_CODE_VALUES:
            return False
    # "00000"/"111111" is almost always a placeholder or a formatting artefact, not a real code.
    return not (len(set(token)) == 1 and len(token) >= 5)


def extract_verification_code(
    *,
    text: str | None = None,
    html: str | None = None,
    min_confidence: float = 0.0,
) -> ExtractionResult:
    """Extract the most likely verification code from a message.

    Args:
        text: The plain-text body.
        html: The HTML body, if any. Its visible text is used as a fallback when ``text`` yields
            nothing, because some senders only populate one part.
        min_confidence: Candidates below this confidence are ignored.

    Returns:
        An :class:`ExtractionResult`; ``code`` is ``None`` when nothing plausible was found.
    """
    sources: list[tuple[str, str]] = []
    if text and text.strip():
        sources.append(("text", text))
    if html and html.strip():
        html_text = html_to_text(html)
        if html_text:
            sources.append(("html", html_text))

    candidates: list[CodeCandidate] = []
    for source_name, content in sources:
        candidates.extend(_scan(content, source_name))

    if not candidates:
        return ExtractionResult(code=None, confidence=0.0, strategy=None, candidates=())

    ranked = tuple(sorted(candidates, key=lambda item: (-item.confidence, len(item.code))))
    best = next((item for item in ranked if item.confidence >= min_confidence), None)
    if best is None:
        return ExtractionResult(code=None, confidence=0.0, strategy=None, candidates=ranked)
    return ExtractionResult(
        code=best.code, confidence=best.confidence, strategy=best.strategy, candidates=ranked
    )


def _scan(content: str, source: str) -> list[CodeCandidate]:
    """Run every strategy over one text source."""
    found: list[CodeCandidate] = []

    for match in _LABELED.finditer(content):
        token = _normalize(match.group(1))
        if _is_plausible(token, standalone=False):
            found.append(CodeCandidate(code=token, confidence=0.9, strategy=f"labelled:{source}"))

    for match in _SEPARATED_DIGITS.finditer(content):
        token = f"{match.group(1)}{match.group(2)}"
        # Not treated as "standalone": the label immediately before it is the evidence, so the
        # placeholder list (which exists to reject bare "123456" in prose) does not apply.
        if _is_plausible(token, standalone=False):
            found.append(CodeCandidate(code=token, confidence=0.8, strategy=f"separated:{source}"))

    for match in _NEAR_LABEL_DIGITS.finditer(content):
        token = match.group(1)
        if _is_plausible(token, standalone=False):
            found.append(
                CodeCandidate(code=token, confidence=0.65, strategy=f"near-label:{source}")
            )

    for match in _STANDALONE_DIGITS.finditer(content):
        token = match.group(1)
        if _is_plausible(token, standalone=True):
            found.append(
                CodeCandidate(code=token, confidence=0.45, strategy=f"standalone:{source}")
            )

    return found


def codes_in_text(content: str) -> list[str]:
    """Return every plausible code token, in order of appearance.

    Useful for debugging and for tests; the extraction entry point is
    :func:`extract_verification_code`.
    """
    matches = [match.group(1) for match in _LABELED.finditer(content)]
    return [
        code
        for code in (_normalize(match) for match in matches)
        if _is_plausible(code, standalone=False)
    ]

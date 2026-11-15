"""Parsing and validating test identities supplied as CSV or plain text.

The brief asks for "import test email addresses from CSV". That is supported, with one hard rule:
**an identity must be an address on a domain this NEXUS instance can actually receive mail on.**
Otherwise the verification email would go somewhere we cannot read, and the test would fail with
a misleading "email timeout". Refusing the input up front is the honest behaviour.

Identities are synthetic by construction: they are either generated inboxes or addresses on a
domain the operator has registered as theirs. Real people's addresses are not usable here, because
the owner cannot read their mail - which is also why NEXUS never needs them.
"""

from __future__ import annotations

import csv
import io
import re

from app.core.exceptions import ValidationError

#: Maximum identities one campaign may contain. A QA run, not a mass-signup operation.
MAX_IDENTITIES_PER_CAMPAIGN = 500

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Header cells recognized as "this column holds the address".
ADDRESS_HEADERS = frozenset({"email", "e-mail", "address", "mail"})


def parse_identity_text(content: str, *, limit: int = MAX_IDENTITIES_PER_CAMPAIGN) -> list[str]:
    """Extract addresses from CSV text or a plain list.

    Accepts a header row (``email`` / ``address`` / …), a single-column CSV, or one address per
    line. Blank lines and ``#`` comments are ignored.

    Args:
        content: The supplied text.
        limit: Maximum number of identities accepted.

    Returns:
        Lowercased, de-duplicated addresses in input order.

    Raises:
        ValidationError: the content is empty, contains no usable address, exceeds ``limit``, or
            contains a malformed address (with the line number).
    """
    if not content or not content.strip():
        raise ValidationError("No identities were supplied.")

    rows = _rows(content)
    if not rows:
        raise ValidationError("No identities were supplied.")

    # Skip a header row when the first cell names an address column.
    first_cells = [cell.strip().lower() for cell in rows[0][1]]
    if first_cells and first_cells[0] in ADDRESS_HEADERS:
        rows = rows[1:]

    seen: set[str] = set()
    addresses: list[str] = []
    problems: list[dict[str, object]] = []

    for line_number, cells in rows:
        raw = (cells[0] if cells else "").strip()
        if not raw or raw.startswith("#"):
            continue
        address = raw.lower()
        if not _EMAIL_RE.match(address):
            problems.append({"line_number": line_number, "reason": "not a valid email address"})
            continue
        if address in seen:
            continue
        seen.add(address)
        addresses.append(address)

    if problems:
        raise ValidationError(
            f"{len(problems)} identity row(s) are invalid.",
            details={"problems": problems[:10]},
        )
    if not addresses:
        raise ValidationError("No identities were supplied.")
    if len(addresses) > limit:
        raise ValidationError(
            f"A campaign may contain at most {limit} identities.",
            details={"requested": len(addresses), "limit": limit},
        )
    return addresses


def _rows(content: str) -> list[tuple[int, list[str]]]:
    """Return ``(line_number, cells)`` pairs, tolerant of plain text input."""
    text = content.strip()
    # A CSV row needs a comma; otherwise treat the input as one address per line.
    if "," not in text:
        return [(index + 1, [line]) for index, line in enumerate(text.splitlines()) if line.strip()]
    try:
        parsed = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ValidationError(f"The identity list could not be read: {exc}") from exc
    return [
        (index + 1, cells) for index, cells in enumerate(parsed) if any(c.strip() for c in cells)
    ]


def domain_of(address: str) -> str:
    """Return the lowercased domain part of an address."""
    return address.rsplit("@", 1)[-1].lower()


def local_part_of(address: str) -> str:
    """Return the lowercased local part of an address."""
    return address.rsplit("@", 1)[0].lower()

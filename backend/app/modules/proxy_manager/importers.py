"""Importers for proxy lists (TXT and CSV).

Design constraints that come straight from the security model:

* **Input is inert data.** Files are read as text and parsed with :mod:`csv` and string
  operations. There is no code execution, no template expansion and no shell involvement
  (threat T6).
* **Size is capped before parsing.** A 500 MB "proxy list" must fail cheaply, not exhaust memory
  while being tokenized (threat T7).
* **Rejection reasons never quote a password.** A rejected line may contain
  ``user:hunter2@host:port``, so the excerpt is sanitized by :func:`_safe_excerpt` before it can
  reach an API response or a log file (threat T3).
* **Line numbers are reported**, because "one of your 4,000 lines is wrong" is not actionable.

The importer does not touch the database. It returns a structured outcome, and dedupe against
already-stored proxies happens in the service layer, where the database is available.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from app.core.exceptions import ImportFileError
from app.core.logging import REDACTED
from app.modules.proxy_manager.parser import (
    MAX_ENTRY_LENGTH,
    ParsedProxy,
    parse_proxy_entry,
)

#: Line prefixes treated as comments, so a documented list can be pasted as-is.
COMMENT_PREFIXES: tuple[str, ...] = ("#", "//", ";")

#: Column aliases accepted in a CSV header. Keys are lowercase header cells.
COLUMN_ALIASES: dict[str, str] = {
    "protocol": "protocol",
    "scheme": "protocol",
    "type": "protocol",
    "host": "host",
    "ip": "host",
    "address": "host",
    "server": "host",
    "port": "port",
    "username": "username",
    "user": "username",
    "login": "username",
    "password": "password",
    "pass": "password",
    "secret": "password",
}


@dataclass(frozen=True, slots=True)
class RejectedEntry:
    """One entry that could not be imported, with a safe explanation."""

    line_number: int
    reason: str
    excerpt: str
    """Sanitized fragment of the offending line. Credentials are replaced by ``***REDACTED***``."""


@dataclass(slots=True)
class ImportOutcome:
    """Result of parsing one list. Counts are derived from the lists below."""

    entries_seen: int = 0
    accepted: list[ParsedProxy] = field(default_factory=list)
    rejected: list[RejectedEntry] = field(default_factory=list)
    duplicates: int = 0

    @property
    def rejected_count(self) -> int:
        """Number of entries that failed to parse or were duplicates."""
        return len(self.rejected) + self.duplicates


def _safe_excerpt(raw: str, limit: int = 80) -> str:
    """Return a short, credential-free fragment of a line for error reporting.

    Anything that looks like ``user:pass@`` or ``host:port:user:pass`` is masked, because a
    rejected line is exactly the kind of string that ends up in a log file or an API response.
    """
    text = raw.strip()
    if "@" in text:
        # Keep the shape, hide the secret half.
        userinfo, _, rest = text.rpartition("@")
        text = f"{REDACTED}@{rest}" if rest else REDACTED
    elif text.count(":") == 3:
        # host:port:user:pass - keep the endpoint, mask the credentials.
        host, port, _user, _password = text.split(":")
        text = f"{host}:{port}:{REDACTED}:{REDACTED}"
    return text[:limit] + ("..." if len(text) > limit else "")


def _is_comment(line: str) -> bool:
    """True for blank lines and comment lines."""
    stripped = line.strip()
    return not stripped or stripped.startswith(COMMENT_PREFIXES)


def _check_size(content: str, max_bytes: int) -> None:
    """Reject an oversized payload before any parsing happens.

    Raises:
        ImportFileError: the content exceeds ``max_bytes`` when UTF-8 encoded.
    """
    size = len(content.encode("utf-8", errors="replace"))
    if size > max_bytes:
        raise ImportFileError(
            f"The proxy list is larger than the {max_bytes} byte limit.",
            details={"size_bytes": size, "limit_bytes": max_bytes},
        )


def _add_entry(
    outcome: ImportOutcome,
    raw: str,
    line_number: int,
    seen: dict[str, ParsedProxy],
) -> None:
    """Parse one entry and record it, or record why it was rejected."""
    if len(raw) > MAX_ENTRY_LENGTH:
        outcome.rejected.append(
            RejectedEntry(
                line_number=line_number,
                reason=f"Entry longer than {MAX_ENTRY_LENGTH} characters",
                excerpt=_safe_excerpt(raw),
            )
        )
        return
    try:
        parsed = parse_proxy_entry(raw)
    except Exception as exc:  # noqa: BLE001 - every parser error is a rejection, never a crash
        reason = getattr(exc, "message", str(exc)) or "Invalid proxy entry"
        outcome.rejected.append(
            RejectedEntry(line_number=line_number, reason=reason, excerpt=_safe_excerpt(raw))
        )
        return

    if parsed.identity_key in seen:
        # Two identical entries in one file are a duplicate, not a second proxy.
        outcome.duplicates += 1
        return
    seen[parsed.identity_key] = parsed
    outcome.accepted.append(parsed)


def import_txt(content: str, *, max_bytes: int = 2_097_152) -> ImportOutcome:
    """Parse a newline-delimited proxy list.

    Args:
        content: The file text.
        max_bytes: Size cap applied before parsing.

    Returns:
        The parsed outcome, including accepted entries, rejections and duplicate count.

    Raises:
        ImportFileError: the content is empty, binary, or too large.
    """
    _check_size(content, max_bytes)
    if "\x00" in content[:4096]:
        raise ImportFileError("The file appears to be binary, not a text proxy list.")

    outcome = ImportOutcome()
    seen: dict[str, ParsedProxy] = {}
    for line_number, line in enumerate(content.splitlines(), start=1):
        if _is_comment(line):
            continue
        outcome.entries_seen += 1
        _add_entry(outcome, line, line_number, seen)
    return outcome


def import_csv(content: str, *, max_bytes: int = 2_097_152) -> ImportOutcome:
    """Parse a CSV proxy list, with or without a header row.

    With a header, column order may be arbitrary and aliases are accepted (``ip`` for ``host``,
    ``user`` for ``username``). Without a header, the column order must be
    ``host,port`` or ``host,port,username,password``.

    Raises:
        ImportFileError: the content is empty, unreadable as CSV, oversized, or header-based with
            no recognizable host column.
    """
    _check_size(content, max_bytes)
    if "\x00" in content[:4096]:
        raise ImportFileError("The file appears to be binary, not a text proxy list.")

    outcome = ImportOutcome()
    seen: dict[str, ParsedProxy] = {}

    try:
        rows = list(csv.reader(io.StringIO(content)))
    except csv.Error as exc:
        raise ImportFileError(f"The CSV file could not be read: {exc}") from exc

    # A line that would make csv.reader raise is not a proxy anyway; skip it loudly.
    rows = [
        (index + 1, [cell.strip() for cell in row])
        for index, row in enumerate(rows)
        if not _is_comment(",".join(row))
    ]
    if not rows:
        raise ImportFileError("The CSV file contains no data rows.")

    header_map = _detect_header(rows[0][1])
    data_rows = rows[1:] if header_map else rows

    # A header row is only recognized when it names a host column. A "header" without one
    # (e.g. ``port,username``) is therefore treated as data and rejected row by row, with line
    # numbers - which tells the operator exactly what happened instead of a vague format error.

    for line_number, cells in data_rows:
        if _is_comment(",".join(cells)):
            continue
        outcome.entries_seen += 1
        entry = _row_to_entry(cells, header_map, line_number, outcome)
        if entry is None:
            continue
        if entry.identity_key in seen:
            outcome.duplicates += 1
            continue
        seen[entry.identity_key] = entry
        outcome.accepted.append(entry)

    return outcome


def _detect_header(first_row: list[str]) -> dict[str, int] | None:
    """Return a column-name -> index map when the first row is a header, else ``None``."""
    normalized = [cell.strip().lower() for cell in first_row]
    mapped: dict[str, int] = {}
    for index, cell in enumerate(normalized):
        canonical = COLUMN_ALIASES.get(cell)
        if canonical and canonical not in mapped:
            mapped[canonical] = index
    # A header must at least identify the host; otherwise it is data.
    return mapped if "host" in mapped else None


def _row_to_entry(
    cells: list[str],
    header_map: dict[str, int] | None,
    line_number: int,
    outcome: ImportOutcome,
) -> ParsedProxy | None:
    """Convert one CSV row to a parsed proxy, recording a rejection on failure."""
    try:
        if header_map is not None:
            raw = _entry_from_mapped_columns(cells, header_map)
        else:
            raw = _entry_from_positional_columns(cells)
        return parse_proxy_entry(raw)
    except Exception as exc:  # noqa: BLE001 - rejections are data, not crashes
        reason = getattr(exc, "message", str(exc)) or "Invalid proxy row"
        outcome.rejected.append(
            RejectedEntry(
                line_number=line_number,
                reason=reason,
                excerpt=_safe_excerpt(",".join(cells)),
            )
        )
        return None


def _entry_from_mapped_columns(cells: list[str], header_map: dict[str, int]) -> str:
    """Rebuild a canonical ``scheme://user:pass@host:port`` string from named columns."""

    def cell(name: str) -> str:
        index = header_map.get(name)
        if index is None or index >= len(cells):
            return ""
        return cells[index].strip()

    host = cell("host")
    port = cell("port")
    if not host:
        raise ImportFileError("The row has no host.")
    if not port:
        raise ImportFileError("The row has no port.")
    protocol = cell("protocol") or "http"
    username = cell("username")
    password = cell("password")

    if username:
        return f"{protocol}://{username}:{password}@{host}:{port}"
    return f"{protocol}://{host}:{port}"


def _entry_from_positional_columns(cells: list[str]) -> str:
    """Rebuild a canonical entry from a headerless row."""
    if len(cells) == 2:  # noqa: PLR2004 - the two documented shapes
        return f"{cells[0]}:{cells[1]}"
    if len(cells) == 4:  # noqa: PLR2004
        return f"{cells[0]}:{cells[1]}:{cells[2]}:{cells[3]}"
    raise ImportFileError(
        f"Expected 2 or 4 columns in a headerless CSV row, found {len(cells)}.",
    )


def detect_format(content: str) -> str:
    """Guess ``csv`` or ``txt`` from the first non-comment line.

    Used when the caller says ``auto``. A line containing a comma and no ``://`` is treated as
    CSV; everything else is treated as a plain list.
    """
    for line in content.splitlines():
        if _is_comment(line):
            continue
        return "csv" if "," in line and "://" not in line else "txt"
    return "txt"


def import_content(content: str, fmt: str, *, max_bytes: int = 2_097_152) -> ImportOutcome:
    """Parse ``content`` as ``txt``, ``csv`` or ``auto``.

    Raises:
        ImportFileError: the format is unknown, or the content fails that parser's checks.
    """
    resolved = (fmt or "auto").strip().lower()
    if resolved == "auto":
        resolved = detect_format(content)
    if resolved == "txt":
        return import_txt(content, max_bytes=max_bytes)
    if resolved == "csv":
        return import_csv(content, max_bytes=max_bytes)
    raise ImportFileError(
        f"Unknown import format {fmt!r}.", details={"allowed": ["auto", "txt", "csv"]}
    )

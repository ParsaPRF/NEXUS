"""Verify that the documentation's status tables agree with the phase sections.

Why this exists
---------------
The README status table, the roadmap status table, and each phase's own heading say the same thing
in three places. Twice during this project they drifted: the detailed section recorded a phase as
complete while the summary table still said "not started", which is what a reader sees first on the
repository page. A hand-maintained summary always drifts eventually; this turns "remember to update
the table" into a check that fails.

Run it:

    uv run python scripts/check_docs.py

Exit code 0 means consistent, 1 means it printed the differences.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows consoles default to a legacy code page and cannot encode the status emoji. Without this
# the checker passed its comparison and then crashed while printing the result - a tool that cannot
# report is not a working tool.
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
ROADMAP = REPO / "docs" / "ROADMAP.md"

#: Any phase heading, e.g. "## Phase 3 - Email Manager ✅" or "## Phase 7 - Finalization ⚠️ mostly
#: complete". The marker is searched for inside the line rather than anchored to its end: an earlier
#: version required the marker to be last, so the one heading with a trailing explanation was
#: silently skipped - and it was Phase 7, the phase whose status mattered most.
HEADING_RE = re.compile(r"^## Phase (?P<number>\d+)\s*[-\u2014]\s*(?P<title>.+)$")
#: Row in a status table: "| 3 | Email Manager | ✅ complete | ... |".
ROW_RE = re.compile(r"^\|\s*(?P<number>\d+)\s*\|(?P<cells>.*)$")

#: Marker -> the markers that are acceptable in a summary table for the same phase.
#: "⚠️" means partly done, so a table may legitimately say either ⚠️ or the word "complete except".
COMPATIBLE: dict[str, set[str]] = {
    "✅": {"✅"},
    "⚠️": {"⚠️", "✅"},
    "🔄": {"🔄", "⚠️"},
    "⬜": {"⬜"},
}


def _status_of(cell: str) -> str | None:
    """Return the first status marker found in a table cell."""
    for marker in ("⚠️", "✅", "🔄", "⬜"):
        if marker in cell:
            return marker
    return None


def phase_headings() -> tuple[dict[int, tuple[str, str]], list[str]]:
    """Return ``({phase_number: (title, marker)}, problems)`` from the roadmap's section headings.

    A heading without a recognizable marker is reported as a problem instead of being skipped:
    silently ignoring a section is how a stale status survives.
    """
    found: dict[int, tuple[str, str]] = {}
    problems: list[str] = []
    for line in ROADMAP.read_text(encoding="utf-8").splitlines():
        match = HEADING_RE.match(line.strip())
        if not match:
            continue
        number = int(match.group("number"))
        title = match.group("title").strip()
        marker = _status_of(title)
        if marker is None:
            problems.append(
                f"docs/ROADMAP.md: phase {number} heading has no status marker "
                f"(use ✅, ⚠️, 🔄 or ⬜): {line.strip()}"
            )
            marker = "?"
        found[number] = (title, marker)
    return found, problems


def status_table(path: Path, headings: dict[int, tuple[str, str]]) -> dict[int, tuple[str, str]]:
    """Return ``{phase_number: (row_text, marker)}`` from a document's phase status table.

    Only rows that look like a phase summary are considered: the README also contains an
    acceptance-criteria table numbered 1..12, so a row qualifies only when its phase number appears
    in ROADMAP as a phase heading.
    """
    rows: dict[int, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ROW_RE.match(line.strip())
        if not match:
            continue
        number = int(match.group("number"))
        if number not in headings:
            continue
        marker = _status_of(match.group("cells"))
        if marker is None:
            continue
        rows.setdefault(number, (line.strip(), marker))
    return rows


def main() -> int:
    """Compare the tables against the headings and report differences."""
    headings, problems = phase_headings()
    if not headings:
        print("FAIL: no phase headings found in docs/ROADMAP.md - the document format changed")
        return 1

    for name, path in (("README.md", README), ("docs/ROADMAP.md", ROADMAP)):
        rows = status_table(path, headings)
        missing = sorted(set(headings) - set(rows))
        if missing:
            problems.append(f"{name}: no status row for phase(s) {missing}")
        for number, (row, marker) in sorted(rows.items()):
            expected = headings[number][1]
            if expected in COMPATIBLE and marker not in COMPATIBLE[expected]:
                problems.append(
                    f"{name}: phase {number} section says {expected} "
                    f"but the table says {marker}\n    {row}"
                )

    # Every phase must be described by a section, and the numbers must be contiguous from 0.
    numbers = sorted(headings)
    if numbers != list(range(len(numbers))):
        problems.append(f"docs/ROADMAP.md: phase numbers are not contiguous from 0: {numbers}")

    if problems:
        print("FAIL: documentation status tables disagree with the phase sections\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nFix the summary table (or the section heading) so the two agree, then run this again."
        )
        return 1

    print(f"OK: {len(headings)} phases, tables in README.md and docs/ROADMAP.md agree")
    for number in sorted(headings):
        title, marker = headings[number]
        print(f"  phase {number}: {marker}  {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""Tests for the documentation consistency checker.

A checker that always passes is worse than none: it creates confidence without evidence. These
tests feed ``scripts/check_docs.py`` deliberately inconsistent documents and assert that it reports
them - so the guard itself is guarded.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[3] / "scripts" / "check_docs.py"


def _load_checker():
    """Import scripts/check_docs.py by path.

    Imported this way because ``scripts/`` is a utility directory, not an installed package: it is
    deliberately not importable from the application, and the tests should not change that.
    """
    spec = importlib.util.spec_from_file_location("check_docs", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROADMAP_TEMPLATE = """# Roadmap

| Phase | Name | Status |
|---|---|---|
| 0 | Discovery | ✅ complete |
| 1 | Foundation | ⚠️ partly |
{extra_rows}
## Phase 0 — Discovery ✅

Body.

## Phase 1 — Foundation ⚠️ mostly complete

Body.
"""

README_TEMPLATE = """# README

| Phase | Scope | Status |
|---|---|---|
| 0 | Discovery | {phase0} |
| 1 | Foundation | {phase1} |
"""


def _write(tmp_path: Path, roadmap: str, readme: str) -> tuple[Path, Path]:
    roadmap_path = tmp_path / "ROADMAP.md"
    readme_path = tmp_path / "README.md"
    roadmap_path.write_text(roadmap, encoding="utf-8")
    readme_path.write_text(readme, encoding="utf-8")
    return readme_path, roadmap_path


def _run(module, tmp_path: Path, roadmap: str, readme: str) -> int:
    """Point the checker at temporary documents and run it."""
    readme_path, roadmap_path = _write(tmp_path, roadmap, readme)
    module.README = readme_path
    module.ROADMAP = roadmap_path
    return module.main()


class TestAgreement:
    """Consistent documents pass."""

    def test_consistent_documents_pass(self, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
        module = _load_checker()
        code = _run(
            module,
            tmp_path,
            ROADMAP_TEMPLATE.format(extra_rows=""),
            README_TEMPLATE.format(phase0="✅ complete", phase1="⚠️ partly"),
        )
        assert code == 0
        assert "agree" in capsys.readouterr().out

    def test_a_warning_section_may_be_summarised_as_complete(self, tmp_path: Path) -> None:
        # ⚠️ means "partly done"; a table that says ✅ alongside an explanation is acceptable.
        module = _load_checker()
        code = _run(
            module,
            tmp_path,
            ROADMAP_TEMPLATE.format(extra_rows=""),
            README_TEMPLATE.format(phase0="✅ complete", phase1="✅ complete except one item"),
        )
        assert code == 0


class TestDriftIsCaught:
    """The staleness this exists to prevent must actually fail."""

    def test_table_says_not_started_but_section_says_done(self, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
        # This is the exact bug that shipped: the README still said "not started" for a phase
        # whose own section said it was complete.
        module = _load_checker()
        code = _run(
            module,
            tmp_path,
            ROADMAP_TEMPLATE.format(extra_rows=""),
            README_TEMPLATE.format(phase0="⬜ not started", phase1="⚠️ partly"),
        )
        assert code == 1
        assert "phase 0" in capsys.readouterr().out

    def test_roadmap_table_disagrees_with_roadmap_section(self, tmp_path: Path) -> None:
        module = _load_checker()
        roadmap = ROADMAP_TEMPLATE.format(extra_rows="").replace(
            "| 0 | Discovery | ✅ complete |", "| 0 | Discovery | ⬜ |"
        )
        code = _run(
            module,
            tmp_path,
            roadmap,
            README_TEMPLATE.format(phase0="✅ complete", phase1="⚠️ partly"),
        )
        assert code == 1

    def test_heading_without_a_marker_is_reported(self, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
        # An earlier version required the marker to end the line and therefore skipped the Phase 7
        # heading entirely - the phase whose status mattered most. It must be an error, not a skip.
        module = _load_checker()
        roadmap = ROADMAP_TEMPLATE.replace(
            "## Phase 1 — Foundation ⚠️ mostly complete", "## Phase 1 — Foundation, nearly there"
        )
        code = _run(
            module,
            tmp_path,
            roadmap,
            README_TEMPLATE.format(phase0="✅ complete", phase1="⚠️ partly"),
        )
        assert code == 1
        assert "no status marker" in capsys.readouterr().out

    def test_missing_status_row_is_reported(self, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
        module = _load_checker()
        readme = """# README

| Phase | Scope | Status |
|---|---|---|
| 0 | Discovery | ✅ |
"""
        code = _run(module, tmp_path, ROADMAP_TEMPLATE.format(extra_rows=""), readme)
        assert code == 1
        assert "no status row" in capsys.readouterr().out

    def test_non_contiguous_phase_numbers_are_reported(self, tmp_path: Path) -> None:
        module = _load_checker()
        roadmap = ROADMAP_TEMPLATE.format(extra_rows="") + "\n## Phase 5 — Gap ✅\n"
        roadmap = roadmap.replace(
            "| 1 | Foundation | ⚠️ partly |",
            "| 1 | Foundation | ⚠️ partly |\n| 5 | Gap | ✅ |",
        )
        code = _run(
            module,
            tmp_path,
            roadmap,
            README_TEMPLATE.format(phase0="✅ complete", phase1="⚠️ partly"),
        )
        assert code == 1


class TestRealRepository:
    """The shipped documentation must pass."""

    def test_the_repository_is_consistent(self) -> None:
        module = _load_checker()
        # Use the real paths the module computed, not the temporary ones.
        assert module.README.exists()
        assert module.ROADMAP.exists()
        assert module.main() == 0

    @pytest.mark.parametrize("path", ["README.md", "docs/ROADMAP.md"])
    def test_both_documents_exist(self, path: str) -> None:
        repo = MODULE_PATH.parents[1]
        assert (repo / path).exists()

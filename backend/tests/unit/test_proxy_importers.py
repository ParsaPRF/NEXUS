"""Importer tests: TXT and CSV parsing, dedupe, limits and redaction.

The importer is the boundary where a user's file becomes database rows, so the tests focus on
what happens to bad input: it must be *counted and explained*, not silently dropped and not able
to crash the request.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ImportFileError
from app.core.logging import REDACTED
from app.modules.proxy_manager.importers import (
    detect_format,
    import_content,
    import_csv,
    import_txt,
)


class TestTxtImport:
    """Newline-delimited lists."""

    def test_parses_valid_entries(self) -> None:
        outcome = import_txt("proxy1.example.test:8080\nproxy2.example.test:3128\n")
        assert outcome.entries_seen == 2
        assert len(outcome.accepted) == 2
        assert outcome.rejected == []
        assert [item.host for item in outcome.accepted] == [
            "proxy1.example.test",
            "proxy2.example.test",
        ]

    def test_skips_comments_and_blank_lines(self) -> None:
        content = (
            "# this is a comment\n"
            "\n"
            "proxy1.example.test:8080\n"
            "// another comment\n"
            "; and another\n"
            "   \n"
            "proxy2.example.test:8081\n"
        )
        outcome = import_txt(content)
        # Comments and blanks are not "entries seen" - they never reached the parser.
        assert outcome.entries_seen == 2
        assert len(outcome.accepted) == 2

    def test_invalid_lines_are_rejected_with_line_numbers(self) -> None:
        content = "proxy1.example.test:8080\nnot-a-proxy\nproxy2.example.test:8081\n"
        outcome = import_txt(content)
        assert outcome.entries_seen == 3
        assert len(outcome.accepted) == 2
        assert len(outcome.rejected) == 1
        assert outcome.rejected[0].line_number == 2

    def test_duplicates_within_one_file_are_counted(self) -> None:
        content = "proxy.example.test:8080\nhttp://proxy.example.test:8080\n"
        outcome = import_txt(content)
        assert len(outcome.accepted) == 1
        assert outcome.duplicates == 1
        assert outcome.rejected == []

    def test_crlf_line_endings_are_handled(self) -> None:
        outcome = import_txt("proxy1.example.test:8080\r\nproxy2.example.test:8081\r\n")
        assert len(outcome.accepted) == 2

    def test_empty_content_yields_nothing(self) -> None:
        outcome = import_txt("")
        assert outcome.entries_seen == 0
        assert outcome.accepted == []

    def test_oversized_content_is_refused_before_parsing(self) -> None:
        with pytest.raises(ImportFileError, match="larger than"):
            import_txt("x" * 500, max_bytes=100)

    def test_binary_content_is_refused(self) -> None:
        with pytest.raises(ImportFileError, match="binary"):
            import_txt("\x00\x01\x02proxy:80")


class TestRejectionSafety:
    """A rejected line is exactly the string that ends up in a log or an API response."""

    def test_password_in_a_rejected_line_is_redacted(self) -> None:
        # 4 fields with a bad port: the row is rejected, and the excerpt must not leak "pw".
        outcome = import_txt("proxy.example.test:notaport:alice:supersecret")
        assert len(outcome.rejected) == 1
        excerpt = outcome.rejected[0].excerpt
        assert "supersecret" not in excerpt
        assert REDACTED in excerpt

    def test_credentials_in_an_unsupported_url_are_redacted(self) -> None:
        outcome = import_txt("gopher://alice:supersecret@proxy.example.test:70")
        assert len(outcome.rejected) == 1
        assert "supersecret" not in outcome.rejected[0].excerpt

    def test_reason_is_specific(self) -> None:
        outcome = import_txt("proxy.example.test")
        assert (
            "port" in outcome.rejected[0].reason.lower()
            or "shape" in outcome.rejected[0].reason.lower()
        )

    def test_long_line_excerpt_is_truncated(self) -> None:
        outcome = import_txt("a" * 400 + ":8080")
        assert len(outcome.rejected[0].excerpt) <= 83  # 80 + "..."


class TestCsvImport:
    """CSV lists, with and without a header."""

    def test_header_with_full_column_names(self) -> None:
        content = (
            "host,port,username,password\n"
            "proxy1.example.test,8080,alice,secret\n"
            "proxy2.example.test,3128,bob,hunter2\n"
        )
        outcome = import_csv(content)
        assert outcome.entries_seen == 2
        assert len(outcome.accepted) == 2
        first = outcome.accepted[0]
        assert (first.host, first.port, first.username, first.password) == (
            "proxy1.example.test",
            8080,
            "alice",
            "secret",
        )

    def test_header_aliases_are_accepted(self) -> None:
        # Columns may appear in any order and use common synonyms.
        content = "scheme,ip,port,user,pass\nsocks5,proxy.example.test,1080,alice,secret\n"
        outcome = import_csv(content)
        assert len(outcome.accepted) == 1
        assert outcome.accepted[0].protocol == "socks5"
        assert outcome.accepted[0].username == "alice"

    def test_header_without_credentials(self) -> None:
        outcome = import_csv("host,port\nproxy.example.test,8080\n")
        assert outcome.accepted[0].has_credentials is False

    def test_headerless_two_columns(self) -> None:
        outcome = import_csv("proxy1.example.test,8080\nproxy2.example.test,3128\n")
        assert len(outcome.accepted) == 2
        assert outcome.accepted[0].protocol == "http"

    def test_headerless_four_columns(self) -> None:
        outcome = import_csv("proxy.example.test,8080,alice,secret\n")
        assert outcome.accepted[0].username == "alice"
        assert outcome.accepted[0].password == "secret"

    def test_wrong_column_count_is_rejected(self) -> None:
        outcome = import_csv("proxy.example.test,8080,extra\n")
        assert len(outcome.accepted) == 0
        assert len(outcome.rejected) == 1
        assert "columns" in outcome.rejected[0].reason.lower()

    def test_a_header_without_a_host_column_is_treated_as_data(self) -> None:
        # Conservative by design: we do not guess. The rows are attempted as data and rejected
        # with line numbers, which is more informative than a vague format error.
        outcome = import_csv("port,username\n8080,alice\n")
        assert outcome.entries_seen == 2
        assert len(outcome.accepted) == 0
        assert len(outcome.rejected) == 2

    def test_bad_port_in_a_row_is_rejected_not_raised(self) -> None:
        outcome = import_csv("host,port\nproxy.example.test,notaport\n")
        assert len(outcome.rejected) == 1
        assert outcome.rejected[0].line_number == 2

    def test_empty_csv_is_refused(self) -> None:
        with pytest.raises(ImportFileError, match="no data rows"):
            import_csv("")

    def test_comment_rows_are_skipped(self) -> None:
        outcome = import_csv("# comment,with,commas\nproxy.example.test,8080\n")
        assert outcome.entries_seen == 1
        assert len(outcome.accepted) == 1

    def test_quoted_fields_are_parsed(self) -> None:
        outcome = import_csv('host,port\n"proxy.example.test",8080\n')
        assert outcome.accepted[0].host == "proxy.example.test"

    def test_oversized_csv_is_refused(self) -> None:
        with pytest.raises(ImportFileError, match="larger than"):
            import_csv("host,port\n" + "a,1\n" * 100, max_bytes=50)


class TestFormatDetection:
    """``auto`` must pick the right parser without guessing wildly."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("proxy.example.test:8080\n", "txt"),
            ("proxy.example.test,8080\n", "csv"),
            ("host,port\nproxy.example.test,8080\n", "csv"),
            ("# comment\n\nproxy.example.test:8080\n", "txt"),
            ("socks5://proxy.example.test:1080\n", "txt"),
        ],
    )
    def test_detection(self, content: str, expected: str) -> None:
        assert detect_format(content) == expected

    def test_import_content_dispatches_to_csv(self) -> None:
        outcome = import_content("host,port\nproxy.example.test,8080\n", "auto")
        assert len(outcome.accepted) == 1

    def test_import_content_dispatches_to_txt(self) -> None:
        outcome = import_content("proxy.example.test:8080\n", "auto")
        assert len(outcome.accepted) == 1

    def test_explicit_format_overrides_detection(self) -> None:
        # A single entry containing a comma in the password must still parse as TXT when forced.
        outcome = import_content("http://alice:a,b@proxy.example.test:8080\n", "txt")
        assert len(outcome.accepted) == 1
        assert outcome.accepted[0].password == "a,b"

    def test_unknown_format_is_refused(self) -> None:
        with pytest.raises(ImportFileError, match="Unknown import format"):
            import_content("proxy:80", "xml")

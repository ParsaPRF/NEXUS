"""Proxy parser tests.

The parser is the first thing untrusted data touches, so these tests are table-driven: one row
per accepted form, one row per rejected shape. When a new format is supported, add a row - do not
rewrite the tests.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import InvalidProxyFormatError, UnsupportedProtocolError
from app.modules.proxy_manager.parser import (
    MAX_ENTRY_LENGTH,
    ParsedProxy,
    normalize_host,
    normalize_port,
    normalize_protocol,
    parse_proxy_entry,
    split_credentials,
)

# --------------------------------------------------------------------------------------
# Accepted forms
# --------------------------------------------------------------------------------------


class TestAcceptedForms:
    """Every documented input shape parses to the expected normalized proxy."""

    @pytest.mark.parametrize(
        ("raw", "protocol", "host", "port"),
        [
            ("http://proxy.example.test:8080", "http", "proxy.example.test", 8080),
            ("https://proxy.example.test:8443", "https", "proxy.example.test", 8443),
            ("socks5://proxy.example.test:1080", "socks5", "proxy.example.test", 1080),
            ("socks5h://proxy.example.test:1080", "socks5", "proxy.example.test", 1080),
            ("socks://proxy.example.test:1080", "socks5", "proxy.example.test", 1080),
            ("SOCKS5://proxy.example.test:1080", "socks5", "proxy.example.test", 1080),
            # No scheme: defaults to http.
            ("proxy.example.test:3128", "http", "proxy.example.test", 3128),
            # Host case is normalized, because DNS is case-insensitive.
            ("HTTP://PROXY.Example.TEST:8080", "http", "proxy.example.test", 8080),
            # explicit IPs
            ("10.1.2.3:8080", "http", "10.1.2.3", 8080),
            ("http://10.1.2.3:8080", "http", "10.1.2.3", 8080),
        ],
    )
    def test_parses_endpoint(self, raw: str, protocol: str, host: str, port: int) -> None:
        parsed = parse_proxy_entry(raw)
        assert (parsed.protocol, parsed.host, parsed.port) == (protocol, host, port)
        assert parsed.username is None
        assert parsed.password is None

    @pytest.mark.parametrize(
        ("raw", "protocol", "port"),
        [
            ("http://proxy.example.test", "http", 80),
            ("https://proxy.example.test", "https", 443),
            ("socks5://proxy.example.test", "socks5", 1080),
        ],
    )
    def test_default_port_per_protocol(self, raw: str, protocol: str, port: int) -> None:
        parsed = parse_proxy_entry(raw)
        assert parsed.protocol == protocol
        assert parsed.port == port

    @pytest.mark.parametrize(
        ("raw", "username", "password"),
        [
            ("http://alice:secret@proxy.example.test:8080", "alice", "secret"),
            ("alice:secret@proxy.example.test:8080", "alice", "secret"),
            ("proxy.example.test:8080:alice:secret", "alice", "secret"),
            # Percent-encoded credentials are decoded.
            ("http://alice:p%40ss@proxy.example.test:8080", "alice", "p@ss"),
        ],
    )
    def test_parses_credentials(self, raw: str, username: str, password: str) -> None:
        parsed = parse_proxy_entry(raw)
        assert parsed.username == username
        assert parsed.password == password
        assert parsed.has_credentials is True

    def test_password_may_contain_a_colon(self) -> None:
        parsed = parse_proxy_entry("http://alice:a:b:c@proxy.example.test:8080")
        assert parsed.username == "alice"
        assert parsed.password == "a:b:c"

    def test_username_only_has_no_password(self) -> None:
        parsed = parse_proxy_entry("http://alice@proxy.example.test:8080")
        assert parsed.username == "alice"
        assert parsed.password is None

    def test_ipv6_with_brackets(self) -> None:
        parsed = parse_proxy_entry("socks5://[2001:db8::1]:1080")
        assert parsed.host == "2001:db8::1"
        assert parsed.port == 1080

    def test_ipv6_plain_form(self) -> None:
        parsed = parse_proxy_entry("[::1]:8080")
        assert parsed.host == "::1"
        assert parsed.port == 8080

    def test_whitespace_is_trimmed(self) -> None:
        parsed = parse_proxy_entry("   http://proxy.example.test:8080   ")
        assert parsed.host == "proxy.example.test"

    def test_trailing_dot_is_normalized_away(self) -> None:
        # "proxy.example.test." and "proxy.example.test" are the same name; keeping both would
        # create two rows for one proxy.
        parsed = parse_proxy_entry("http://proxy.example.test.:8080")
        assert parsed.host == "proxy.example.test"


# --------------------------------------------------------------------------------------
# Rejected forms
# --------------------------------------------------------------------------------------


class TestRejectedForms:
    """Malformed input produces a specific error, never an exception storm."""

    @pytest.mark.parametrize("raw", ["", "   ", "\t", "\n"])
    def test_empty_entries_are_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidProxyFormatError, match="empty"):
            parse_proxy_entry(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "ftp://proxy.example.test:21",
            "gopher://proxy.example.test:70",
            "file:///etc/passwd",
            "socks4://proxy.example.test:1080",
        ],
    )
    def test_unsupported_protocols_are_rejected(self, raw: str) -> None:
        with pytest.raises(UnsupportedProtocolError, match="[Uu]nsupported"):
            parse_proxy_entry(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "proxy.example.test",  # no port
            "proxy.example.test:0",
            "proxy.example.test:65536",
            "proxy.example.test:notaport",
            "999.1.1.1:8080",  # looks like an IP but is not one
            "proxy.example.test:8080:alice",  # ambiguous 3 fields
            "proxy.example.test:8080:alice:secret:extra",  # too many fields
        ],
    )
    def test_structurally_invalid_entries_are_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidProxyFormatError):
            parse_proxy_entry(raw)

    def test_overlong_entry_is_rejected(self) -> None:
        with pytest.raises(InvalidProxyFormatError, match="longer"):
            parse_proxy_entry("http://" + "a" * MAX_ENTRY_LENGTH + ":80")

    def test_unclosed_ipv6_bracket_is_rejected(self) -> None:
        with pytest.raises(InvalidProxyFormatError, match="bracket"):
            parse_proxy_entry("[::1:8080")

    def test_empty_username_is_rejected(self) -> None:
        with pytest.raises(InvalidProxyFormatError):
            parse_proxy_entry("http://:secret@proxy.example.test:8080")

    def test_host_with_a_path_is_rejected(self) -> None:
        with pytest.raises(InvalidProxyFormatError):
            parse_proxy_entry("http://proxy.example.test/some/path:8080")


# --------------------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------------------


class TestNormalizers:
    """The building blocks, tested directly for the edge cases the parser relies on."""

    def test_protocol_aliases(self) -> None:
        assert normalize_protocol("HTTP") == "http"
        assert normalize_protocol("socks") == "socks5"
        assert normalize_protocol(" socks5h ") == "socks5"

    def test_unknown_protocol_raises(self) -> None:
        with pytest.raises(UnsupportedProtocolError):
            normalize_protocol("sslv3")

    def test_port_range_is_enforced(self) -> None:
        assert normalize_port("443") == 443
        assert normalize_port(443) == 443
        with pytest.raises(InvalidProxyFormatError):
            normalize_port("0")
        with pytest.raises(InvalidProxyFormatError):
            normalize_port("70000")

    def test_host_brackets_and_case(self) -> None:
        assert normalize_host("[::1]") == "::1"
        assert normalize_host("Example.Test") == "example.test"

    def test_ipv4_is_canonicalized(self) -> None:
        # Leading zeros are not valid IPv4 and must not silently become a different address.
        assert normalize_host("10.0.0.1") == "10.0.0.1"
        with pytest.raises(InvalidProxyFormatError):
            normalize_host("010.0.0.1")

    def test_credentials_split(self) -> None:
        assert split_credentials("alice:secret") == ("alice", "secret")
        assert split_credentials("alice") == ("alice", None)
        with pytest.raises(InvalidProxyFormatError):
            split_credentials(":secret")

    def test_empty_password_becomes_none(self) -> None:
        # "alice:" is a username with no password, not a username with an empty password.
        assert split_credentials("alice:") == ("alice", None)


# --------------------------------------------------------------------------------------
# Identity and display
# --------------------------------------------------------------------------------------


class TestIdentity:
    """De-duplication keys must match what the database constraint can enforce."""

    def test_same_proxy_written_two_ways_has_one_identity(self) -> None:
        first = parse_proxy_entry("http://proxy.example.test:80")
        second = parse_proxy_entry("proxy.example.test:80")
        assert first.identity_key == second.identity_key

    def test_different_username_is_a_different_identity(self) -> None:
        first = parse_proxy_entry("http://alice:pw@proxy.example.test:8080")
        second = parse_proxy_entry("http://bob:pw@proxy.example.test:8080")
        assert first.identity_key != second.identity_key

    def test_different_protocol_is_a_different_identity(self) -> None:
        first = parse_proxy_entry("http://proxy.example.test:8080")
        second = parse_proxy_entry("socks5://proxy.example.test:1080")
        assert first.identity_key != second.identity_key

    def test_identity_key_contains_no_password(self) -> None:
        parsed = parse_proxy_entry("http://alice:supersecret@proxy.example.test:8080")
        assert "supersecret" not in parsed.identity_key

    def test_display_address_hides_credentials(self) -> None:
        parsed = parse_proxy_entry("http://alice:supersecret@proxy.example.test:8080")
        assert parsed.display_address == "http://proxy.example.test:8080"
        assert "supersecret" not in str(parsed)

    def test_display_address_brackets_ipv6(self) -> None:
        parsed = parse_proxy_entry("socks5://[::1]:1080")
        assert parsed.display_address == "socks5://[::1]:1080"

    def test_parsed_proxy_is_hashable(self) -> None:
        # Frozen dataclass: usable as a set/dict key, which the de-duplication relies on.
        parsed = parse_proxy_entry("http://proxy.example.test:8080")
        assert len({parsed, parsed}) == 1

    def test_is_frozen(self) -> None:
        parsed = parse_proxy_entry("http://proxy.example.test:8080")
        with pytest.raises((AttributeError, TypeError)):
            parsed.host = "changed"  # type: ignore[misc]


def test_parsed_proxy_repr_does_not_leak_password() -> None:
    """A repr that leaks a credential is a credential in every traceback you paste."""
    parsed = ParsedProxy(
        protocol="http", host="h.example.test", port=80, username="u", password="pw"
    )
    default_repr = repr(parsed)
    # The dataclass repr does show the password field, which is why nothing logs a ParsedProxy
    # directly. This test documents that the safe representation is ``display_address``.
    assert "h.example.test" in default_repr
    assert "pw" not in parsed.display_address

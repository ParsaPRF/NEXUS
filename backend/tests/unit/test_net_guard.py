"""SSRF guard tests - the security core of the whole project.

These are the most important unit tests in the codebase. Every case below is a real attack
technique or a real misconfiguration, and a regression here means the server can be made to
talk to something it must not (docs/SECURITY.md T1).

Where a test needs DNS, the resolver is replaced with a fake. Otherwise the suite would depend
on network access and on whatever a name happens to resolve to today - a test that fails because
someone's DNS is flaky teaches people to ignore failures.
"""

from __future__ import annotations

import socket

import pytest

from app.core import net_guard
from app.core.exceptions import SsrfBlockedError, TargetNotAuthorizedError, ValidationError

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _fake_resolver(*addresses: str):
    """Return a ``getaddrinfo`` replacement that always resolves to ``addresses``."""

    def _resolve(host: str, port: int, **kwargs: object) -> list[tuple]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
            for address in addresses
        ]

    return _resolve


class TestSchemeValidation:
    """Only http and https may ever be fetched."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://127.0.0.1:11211/_stats",
            "ftp://example.test/file",
            "dict://example.test:2628/",
            "jar:http://example.test/x.jar!/",
        ],
    )
    def test_dangerous_schemes_are_blocked(self, url: str) -> None:
        with pytest.raises(SsrfBlockedError, match="[Ss]cheme"):
            net_guard.validate_outbound_url(url, resolve=False)

    def test_schemeless_url_is_blocked(self) -> None:
        # "example.test/path" has no scheme; urlsplit treats it as a path, so there is no host.
        with pytest.raises((SsrfBlockedError, ValidationError)):
            net_guard.validate_outbound_url("example.test/path", resolve=False)

    def test_empty_url_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            net_guard.validate_outbound_url("   ", resolve=False)

    def test_http_and_https_are_allowed(self) -> None:
        assert (
            net_guard.validate_outbound_url("http://example.test", resolve=False).scheme == "http"
        )
        assert (
            net_guard.validate_outbound_url("https://example.test", resolve=False).scheme == "https"
        )

    def test_require_https_rejects_plain_http(self) -> None:
        with pytest.raises(SsrfBlockedError, match="https"):
            net_guard.validate_outbound_url(
                "http://example.test", require_https=True, resolve=False
            )


class TestBlockedHostnames:
    """Names that mean 'this machine' or 'internal infrastructure'."""

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "LOCALHOST",  # case must not matter
            "localhost.localdomain",
            "metadata.google.internal",
            "instance-data",
            "kubernetes.default",
        ],
    )
    def test_blocked_hostnames_are_refused(self, host: str) -> None:
        with pytest.raises(SsrfBlockedError):
            net_guard.validate_outbound_url(f"http://{host}/", resolve=False)

    @pytest.mark.parametrize("host", ["db.local", "api.internal", "printer.localhost"])
    def test_blocked_suffixes_are_refused(self, host: str) -> None:
        with pytest.raises(SsrfBlockedError):
            net_guard.validate_outbound_url(f"http://{host}/", resolve=False)

    def test_trailing_dot_is_normalized(self) -> None:
        # "localhost." is the same name as "localhost" and must not slip through.
        with pytest.raises(SsrfBlockedError):
            net_guard.validate_outbound_url("http://localhost./", resolve=False)


class TestBlockedAddressRanges:
    """Literal IP addresses that must never be contacted."""

    @pytest.mark.parametrize(
        ("address", "reason"),
        [
            ("127.0.0.1", "loopback"),
            ("127.1.2.3", "loopback"),
            ("10.0.0.5", "private"),
            ("172.16.4.4", "private"),
            ("192.168.1.1", "private"),
            ("169.254.169.254", "metadata"),
            ("0.0.0.0", "unspecified"),
            ("224.0.0.1", "multicast"),
            ("240.0.0.1", "reserved"),
            ("[::1]", "loopback"),
            ("[fe80::1]", "link-local"),
            ("[fc00::1]", "private"),
            ("[::]", "unspecified"),
        ],
    )
    def test_blocked_addresses_are_refused(self, address: str, reason: str) -> None:
        url = f"http://{address}/" if address.startswith("[") else f"http://{address}/"
        with pytest.raises(SsrfBlockedError) as excinfo:
            net_guard.validate_outbound_url(url, resolve=False)
        # The reason must be specific: it is what the operator sees in the audit log.
        assert reason in str(excinfo.value).lower()

    def test_ipv4_mapped_ipv6_loopback_is_refused(self) -> None:
        # ::ffff:127.0.0.1 is loopback wearing an IPv6 costume.
        with pytest.raises(SsrfBlockedError):
            net_guard.validate_outbound_url("http://[::ffff:127.0.0.1]/", resolve=False)

    def test_public_address_is_allowed(self) -> None:
        validated = net_guard.validate_outbound_url("https://93.184.216.34/", resolve=False)
        assert validated.hostname == "93.184.216.34"
        assert validated.resolved_ips == ("93.184.216.34",)


class TestDnsBasedChecks:
    """A public-looking name that resolves to a private address is the DNS-rebinding case."""

    def test_hostname_resolving_to_private_address_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("10.1.2.3"))
        with pytest.raises(SsrfBlockedError, match="resolves to a blocked address"):
            net_guard.validate_outbound_url("http://internal-looking.example.test/")

    def test_hostname_resolving_to_loopback_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("127.0.0.1"))
        with pytest.raises(SsrfBlockedError):
            net_guard.validate_outbound_url("http://sneaky.example.test/")

    def test_one_bad_answer_among_several_is_enough_to_refuse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hostile resolver can return a public address first and a private one second; we
        # must judge *every* answer, not just the first.
        monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("93.184.216.34", "10.0.0.9"))
        with pytest.raises(SsrfBlockedError):
            net_guard.validate_outbound_url("http://mixed.example.test/")

    def test_public_resolution_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("93.184.216.34"))
        validated = net_guard.validate_outbound_url("https://example.test/")
        assert validated.resolved_ips == ("93.184.216.34",)
        assert validated.port == 443

    def test_unresolvable_host_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise socket.gaierror("name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        with pytest.raises(SsrfBlockedError, match="could not be resolved"):
            net_guard.validate_outbound_url("http://does-not-exist.example.test/")


class TestPrivateTargetEscapeHatch:
    """The development opt-in relaxes private addresses but NOT metadata addresses."""

    def test_private_address_allowed_when_enabled(self) -> None:
        # The demo target runs on loopback, so the escape hatch must cover loopback - otherwise
        # Phase 4 could not test the bundled application at all.
        validated = net_guard.validate_outbound_url(
            "http://127.0.0.1:8000/", allow_private=True, resolve=False
        )
        assert validated.port == 8000

    def test_private_network_address_allowed_when_enabled(self) -> None:
        validated = net_guard.validate_outbound_url(
            "http://10.0.0.7:9000/", allow_private=True, resolve=False
        )
        assert validated.hostname == "10.0.0.7"

    def test_metadata_address_still_refused_when_private_allowed(self) -> None:
        # Nothing legitimate points at the cloud metadata service, not even in development.
        with pytest.raises(SsrfBlockedError, match="metadata"):
            net_guard.validate_outbound_url(
                "http://169.254.169.254/latest/meta-data/", allow_private=True, resolve=False
            )

    def test_multicast_still_refused_when_private_allowed(self) -> None:
        with pytest.raises(SsrfBlockedError):
            net_guard.validate_outbound_url("http://224.0.0.1/", allow_private=True, resolve=False)


class TestUrlStructure:
    """Port and host parsing problems surface as validation errors, not crashes."""

    def test_invalid_port_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="port"):
            net_guard.validate_outbound_url("http://example.test:99999/", resolve=False)

    def test_missing_host_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="host"):
            net_guard.validate_outbound_url("http:///path", resolve=False)

    def test_default_ports_are_filled_in(self) -> None:
        assert net_guard.validate_outbound_url("http://example.test", resolve=False).port == 80
        assert net_guard.validate_outbound_url("https://example.test", resolve=False).port == 443

    def test_host_header_omits_default_port(self) -> None:
        assert (
            net_guard.validate_outbound_url("https://example.test", resolve=False).host_header
            == "example.test"
        )

    def test_host_header_includes_non_default_port(self) -> None:
        assert (
            net_guard.validate_outbound_url("https://example.test:8443", resolve=False).host_header
            == "example.test:8443"
        )

    def test_whitespace_is_trimmed(self) -> None:
        validated = net_guard.validate_outbound_url("  https://example.test/  ", resolve=False)
        assert validated.hostname == "example.test"


class TestTargetAllowlist:
    """Registration targets pass two independent gates: allowlist AND SSRF guard."""

    def test_empty_allowlist_permits_nothing(self) -> None:
        # Deny-by-default. An unconfigured instance must not be able to reach any target.
        with pytest.raises(TargetNotAuthorizedError, match="No authorized target hosts"):
            net_guard.is_target_authorized("https://app.example.test/", allowed_hosts=[])

    def test_host_outside_allowlist_is_refused(self) -> None:
        with pytest.raises(TargetNotAuthorizedError, match="not in the authorized target"):
            net_guard.is_target_authorized(
                "https://evil.example.test/", allowed_hosts=["app.example.test"]
            )

    def test_allowlisted_host_is_permitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("93.184.216.34"))
        validated = net_guard.is_target_authorized(
            "https://app.example.test/", allowed_hosts=["app.example.test"]
        )
        assert validated.hostname == "app.example.test"

    def test_allowlist_allows_case_and_trailing_dot_variations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver("93.184.216.34"))
        validated = net_guard.is_target_authorized(
            "https://App.Example.Test/", allowed_hosts=["app.example.test."]
        )
        assert validated.hostname == "app.example.test"

    def test_allowlisted_host_still_fails_ssrf_guard(self) -> None:
        # Being on the allowlist does not exempt a host from the address checks: an operator
        # could otherwise authorize "metadata" by mistake.
        with pytest.raises(SsrfBlockedError):
            net_guard.is_target_authorized(
                "http://169.254.169.254/", allowed_hosts=["169.254.169.254"]
            )

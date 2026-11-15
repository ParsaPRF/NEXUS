"""SSRF and target-allowlist guard - the single choke point for user-supplied URLs.

The threat
----------
NEXUS fetches URLs that the *user* supplies: proxy import sources, a health-check endpoint, and
registration target base URLs. If the server fetches such a URL blindly, an attacker (or a
careless paste) can make it talk to things only the server can reach:

* ``http://127.0.0.1:5432``      - the local database
* ``http://10.0.0.5/admin``      - a machine on the LAN
* ``http://169.254.169.254/...`` - the cloud instance metadata service (credential theft)
* ``file:///etc/passwd``         - local file disclosure

This is Server-Side Request Forgery (SSRF), threat T1.

The defence
-----------
One module, :func:`validate_outbound_url`, applied *before* any outbound request. It checks:

1. **Scheme** - only ``http`` and ``https``.
2. **Hostname deny-list** - ``localhost``, ``*.local``, metadata hostnames.
3. **Literal IP check** - loopback, private, link-local, multicast, reserved, unspecified;
   IPv4 and IPv6, including IPv4-mapped IPv6 (``::ffff:127.0.0.1``).
4. **DNS re-check** - resolve the hostname and re-apply the IP rules to *every* answer. This
   closes DNS rebinding, where a public-looking name resolves to a private address.
5. **Port sanity** and a **response size cap** via :func:`read_capped`.

A single development escape hatch (``allow_private=True``) exists because testing against a
local demo target is a legitimate need. It is wired to ``NEXUS_NETWORK__ALLOW_PRIVATE_TARGETS``,
defaults to ``False``, and is refused outright in the production profile.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.core.exceptions import (
    SsrfBlockedError,
    TargetNotAuthorizedError,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing httpx at module import
    from collections.abc import Iterable

    import httpx

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Hostnames that are refused regardless of what DNS says.
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "kubernetes.default",
        "kubernetes.default.svc",
    }
)

#: Suffixes that must never be resolved (internal discovery names).
BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (".local", ".internal", ".localhost", ".home.arpa")

#: Link-local / metadata addresses, refused even in "allow private" development mode.
#: A developer might legitimately point at 127.0.0.1; nobody legitimately points at the
#: cloud metadata service.
BLOCKED_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / OpenStack metadata
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud metadata
        "fd00:ec2::254",  # AWS IMDSv6
    }
)

MAX_PORT = 65535

#: Block reasons that the development escape hatch (``allow_private=True``) may override.
#: Metadata, multicast and reserved addresses are deliberately absent: no local development
#: target lives at 169.254.169.254.
_PRIVATE_ESCAPE_REASONS: frozenset[str] = frozenset({"private address", "loopback address"})


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    """A URL that passed every guard, plus the addresses it resolved to.

    Returning the resolved IPs lets a caller pin the connection (connect to the IP that was
    validated rather than resolving a second time, which would reopen the rebinding window).
    """

    url: str
    scheme: str
    hostname: str
    port: int
    resolved_ips: tuple[str, ...]

    @property
    def host_header(self) -> str:
        """The value to send as the HTTP ``Host`` header."""
        default = 443 if self.scheme == "https" else 80
        return self.hostname if self.port == default else f"{self.hostname}:{self.port}"


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a reason string when ``ip`` must not be contacted, else ``None``.

    Order matters: metadata addresses are checked first so the reason is specific and useful
    in the audit log and in error messages.
    """
    if str(ip) in BLOCKED_ADDRESSES:
        return "cloud metadata address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_reserved:
        return "reserved address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ``::ffff:127.0.0.1`` must be judged as the IPv4 address it really is.
        return _is_blocked_address(ip.ipv4_mapped)
    if ip.is_private:
        return "private address"
    return None


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP literal, tolerating bracketed IPv6. ``None`` if not an IP."""
    candidate = host.strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve ``hostname`` to a tuple of IP strings.

    Uses ``getaddrinfo`` so both A and AAAA records are returned - checking only IPv4 would
    leave the IPv6 path open.

    Raises:
        SsrfBlockedError: if the name cannot be resolved at all. Failing closed is intentional:
            an unresolvable name is not a URL we can safely vouch for.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfBlockedError(
            f"Host {hostname!r} could not be resolved.",
            details={"hostname": hostname, "reason": "dns_failure"},
        ) from exc

    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SsrfBlockedError(
            f"Host {hostname!r} resolved to no addresses.",
            details={"hostname": hostname, "reason": "no_addresses"},
        )
    return tuple(addresses)


def validate_outbound_url(
    url: str,
    *,
    allow_private: bool = False,
    require_https: bool = False,
    resolve: bool = True,
) -> ValidatedUrl:
    """Validate a user-supplied outbound URL, or raise :class:`SsrfBlockedError`.

    Args:
        url: The candidate URL.
        allow_private: Development escape hatch. Permits **private and loopback** addresses so
            a locally running demo target (``http://127.0.0.1:9000``) can be tested. It never
            permits metadata, multicast or reserved addresses, because nothing legitimate needs
            those.
        require_https: Set for registration targets where transport encryption is expected.
        resolve: When ``False``, skip the DNS step. Only for tests that check parsing alone;
            production code must leave this at the default.

    Returns:
        A :class:`ValidatedUrl` describing the checked URL.

    Raises:
        SsrfBlockedError: the URL is malformed, uses a blocked scheme/hostname/address, or
            resolves to a blocked address.
        ValidationError: the URL is structurally unusable (no host, bad port).
    """
    if not url or not url.strip():
        raise ValidationError("A URL is required.", details={"field": "url"})

    candidate = url.strip()
    parts = urlsplit(candidate)

    # 1. Scheme ---------------------------------------------------------------------
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SsrfBlockedError(
            f"Scheme {scheme or '(none)'!r} is not allowed; only http and https are permitted.",
            details={"url": candidate, "scheme": scheme, "reason": "scheme_not_allowed"},
        )
    if require_https and scheme != "https":
        raise SsrfBlockedError(
            "This target requires https.",
            details={"url": candidate, "scheme": scheme, "reason": "https_required"},
        )

    # 2. Host presence and port -----------------------------------------------------
    hostname = (parts.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ValidationError("The URL has no host.", details={"url": candidate})
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValidationError("The URL has an invalid port.", details={"url": candidate}) from exc
    if not 1 <= port <= MAX_PORT:
        raise ValidationError(
            "The URL port is out of range.", details={"url": candidate, "port": port}
        )

    # 3. Hostname deny-list ---------------------------------------------------------
    if hostname in BLOCKED_HOSTNAMES:
        raise SsrfBlockedError(
            f"Host {hostname!r} is blocked.",
            details={"hostname": hostname, "reason": "blocked_hostname"},
        )
    if any(hostname.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES):
        raise SsrfBlockedError(
            f"Host {hostname!r} is blocked (internal name suffix).",
            details={"hostname": hostname, "reason": "blocked_host_suffix"},
        )

    # 4. Literal IP, then DNS answers ----------------------------------------------
    literal = _parse_ip(hostname)
    if literal is not None:
        reason = _is_blocked_address(literal)
        if reason and not (allow_private and reason in _PRIVATE_ESCAPE_REASONS):
            raise SsrfBlockedError(
                f"Address {literal} is blocked ({reason}).",
                details={"hostname": hostname, "address": str(literal), "reason": reason},
            )
        return ValidatedUrl(candidate, scheme, hostname, port, (str(literal),))

    addresses: tuple[str, ...] = ()
    if resolve:
        addresses = resolve_host(hostname, port)
        for address in addresses:
            parsed = _parse_ip(address)
            if parsed is None:  # pragma: no cover - getaddrinfo always yields literals
                continue
            reason = _is_blocked_address(parsed)
            if reason is None:
                continue
            # In development mode a *deliberate* private or loopback address is tolerated, so a
            # demo target on 127.0.0.1 works. The value must be the literal the caller typed -
            # a hostname that merely *resolves* to loopback is still refused, because that is
            # what a DNS-rebinding attack looks like.
            if allow_private and reason in _PRIVATE_ESCAPE_REASONS and str(parsed) == hostname:
                continue
            raise SsrfBlockedError(
                f"Host {hostname!r} resolves to a blocked address ({reason}).",
                details={"hostname": hostname, "address": address, "reason": reason},
            )

    return ValidatedUrl(candidate, scheme, hostname, port, addresses)


def is_target_authorized(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allow_private: bool = False,
) -> ValidatedUrl:
    """Validate a **registration target** URL: a deny-by-default allowlist plus the SSRF guard.

    Two independent gates protect this operation on purpose (docs/SECURITY.md 3):

    * the host must be in ``NEXUS_NETWORK__ALLOWED_TARGET_HOSTS``, and
    * the URL must pass the SSRF guard.

    An empty allowlist means *no external target is permitted at all*; only the bundled demo
    application, which is used through a local adapter, may run.

    **Order matters.** The allowlist is checked first, using string comparison only. It is a
    cheap, deterministic policy decision, whereas the SSRF guard has to resolve DNS. Evaluating
    the guard first would mean an unauthorized host could be *resolved* before we decided we
    were never going to talk to it - needless outbound traffic driven by an untrusted value.

    Raises:
        TargetNotAuthorizedError: the host is not on the allowlist.
        SsrfBlockedError: the URL fails the guard.
    """
    normalized = {host.strip().lower().rstrip(".") for host in allowed_hosts if host.strip()}

    # Parse just enough to learn the host without resolving anything.
    candidate = (url or "").strip()
    parts = urlsplit(candidate)
    hostname = (parts.hostname or "").strip().lower().rstrip(".")

    if not normalized:
        raise TargetNotAuthorizedError(
            "No authorized target hosts are configured; only the bundled demo application "
            "may be used. Add the host to NEXUS_NETWORK__ALLOWED_TARGET_HOSTS to authorize one.",
            details={"host": hostname or candidate},
        )
    if hostname not in normalized:
        raise TargetNotAuthorizedError(
            f"Host {hostname!r} is not in the authorized target allowlist.",
            details={"host": hostname, "allowlist": sorted(normalized)},
        )

    return validate_outbound_url(candidate, allow_private=allow_private)


async def read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a streaming response but stop after ``max_bytes``.

    A hostile or misconfigured peer can otherwise return a multi-gigabyte body and exhaust
    memory (threat T7). Read in chunks so the cap applies during transfer, not after it.

    Raises:
        ValidationError: the body exceeded the cap.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError(
                f"Response exceeded the {max_bytes} byte limit.",
                details={"limit_bytes": max_bytes},
            )
        chunks.append(chunk)
    return b"".join(chunks)

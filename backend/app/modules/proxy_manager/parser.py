"""Proxy entry parsing and normalization.

The parser accepts the formats proxy lists actually use and produces one normalized
:class:`ParsedProxy`. Two things matter more than the parsing itself:

1. **It treats input as inert data.** There is no ``eval``, no ``exec``, no shell call and no
   template expansion anywhere near imported content (threat T6). A malformed line produces a
   rejection with a reason, never an exception that escapes to the caller.

2. **It never echoes credentials back.** Rejection reasons and excerpts are built so a password
   cannot leak into a log line, an API response or an audit row (threat T3).

Accepted forms:

===========================================  ==================================================
``scheme://host:port``                       explicit scheme
``scheme://user:pass@host:port``             proxied with credentials
``host:port``                                scheme defaults to ``http``
``host:port:user:pass``                      common in exported proxy lists
``user:pass@host:port``                      credentials without a scheme
===========================================  ==================================================

Anything else (missing port, ambiguous field count, non-numeric port, unsupported scheme) is
rejected with a specific reason.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.core.exceptions import InvalidProxyFormatError, UnsupportedProtocolError
from app.models.enums import ProxyProtocol

#: Default port per protocol. Used only when the entry omits the port explicitly.
DEFAULT_PORTS: dict[str, int] = {
    ProxyProtocol.HTTP.value: 80,
    ProxyProtocol.HTTPS.value: 443,
    ProxyProtocol.SOCKS5.value: 1080,
}

#: Accepted protocol spellings mapped to the canonical value.
PROTOCOL_ALIASES: dict[str, str] = {
    "http": ProxyProtocol.HTTP.value,
    "https": ProxyProtocol.HTTPS.value,
    "socks": ProxyProtocol.SOCKS5.value,
    "socks5": ProxyProtocol.SOCKS5.value,
    "socks5h": ProxyProtocol.SOCKS5.value,  # curl spelling: resolve DNS through the proxy
}

#: Upper bound on a single entry. A "proxy" longer than this is garbage or an attack (T7).
MAX_ENTRY_LENGTH = 512

#: Longest plausible hostname (RFC 1035 limits the whole name to 253 characters).
MAX_HOST_LENGTH = 255

_MIN_PORT = 1
_MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class ParsedProxy:
    """A normalized proxy endpoint.

    Frozen so it can be used as a dictionary key and cannot be mutated after validation - the
    value that reached the database is the value that was checked.
    """

    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def has_credentials(self) -> bool:
        """True when the proxy requires a username and password."""
        return bool(self.username)

    @property
    def identity_key(self) -> str:
        """Stable de-duplication key.

        The database's ``UNIQUE(protocol, host, port, username)`` cannot de-duplicate rows whose
        ``username`` is NULL (SQL treats NULLs as distinct), so imports also de-duplicate in
        application code with this key, where a missing username is the empty string.
        """
        return f"{self.protocol}|{self.host}|{self.port}|{self.username or ''}"

    @property
    def display_address(self) -> str:
        """``scheme://host:port`` with credentials omitted - safe for logs and exports."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.protocol}://{host}:{self.port}"

    def __str__(self) -> str:
        return self.display_address


def normalize_protocol(raw: str) -> str:
    """Map a protocol spelling to its canonical value.

    Raises:
        UnsupportedProtocolError: the scheme is not http/https/socks5.
    """
    protocol = PROTOCOL_ALIASES.get(raw.strip().lower())
    if protocol is None:
        raise UnsupportedProtocolError(f"Unsupported proxy protocol: {raw!r}.")
    return protocol


def normalize_host(raw: str) -> str:
    """Lowercase, strip brackets and validate a host.

    IP literals are canonicalized (``[::1]`` -> ``::1``, ``010.1.2.3`` rejected as malformed), so
    the same proxy written two ways produces one identity key.

    Raises:
        InvalidProxyFormatError: the host is empty, contains illegal characters, or looks like
            a malformed IP literal.
    """
    host = raw.strip().strip("[]").lower().rstrip(".")
    if not host:
        raise InvalidProxyFormatError("The proxy entry has no host.")
    if len(host) > MAX_HOST_LENGTH:
        raise InvalidProxyFormatError("The proxy host is too long.")
    if any(character in host for character in " /\\?#@"):
        # '@' here would mean a second credential separator, which means the entry was parsed
        # into the wrong fields - better to reject than to guess.
        raise InvalidProxyFormatError(f"Invalid characters in the proxy host: {host!r}.")

    # If the text looks like an IP literal, require that it parses as one. Otherwise "999.1.1.1"
    # would be accepted as a "hostname" and fail confusingly at connect time instead of at import.
    looks_like_ipv4 = "." in host and all(part.isdigit() for part in host.split("."))
    if looks_like_ipv4 or ":" in host:
        try:
            return str(ipaddress.ip_address(host))
        except ValueError as exc:
            raise InvalidProxyFormatError(
                f"The proxy host looks like an IP address but is invalid: {host!r}."
            ) from exc
    return host


def normalize_port(raw: str | int) -> int:
    """Validate a port number.

    Raises:
        InvalidProxyFormatError: the port is not an integer in ``1..65535``.
    """
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise InvalidProxyFormatError(f"The proxy port is not a number: {raw!r}.") from exc
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise InvalidProxyFormatError(f"The proxy port is out of range: {port}.")
    return port


def split_credentials(raw: str) -> tuple[str | None, str | None]:
    """Split ``user:pass`` into its parts, tolerating a password containing colons.

    Raises:
        InvalidProxyFormatError: the username is empty.
    """
    if ":" not in raw:
        username, password = raw, None
    else:
        username, _, password = raw.partition(":")
    username = unquote(username).strip()
    if not username:
        raise InvalidProxyFormatError("The proxy entry has an empty username.")
    password = unquote(password).strip() if password is not None else None
    return username, password or None


def parse_proxy_entry(raw: str) -> ParsedProxy:
    """Parse one proxy entry into a normalized :class:`ParsedProxy`.

    Args:
        raw: A single line or CSV row, already stripped of surrounding whitespace.

    Returns:
        The normalized proxy.

    Raises:
        InvalidProxyFormatError: the entry is empty, too long, ambiguous, or structurally invalid.
        UnsupportedProtocolError: the scheme is not http/https/socks5.
    """
    if raw is None:
        raise InvalidProxyFormatError("The proxy entry is empty.")
    entry = raw.strip()
    if not entry:
        raise InvalidProxyFormatError("The proxy entry is empty.")
    if len(entry) > MAX_ENTRY_LENGTH:
        raise InvalidProxyFormatError(
            f"The proxy entry is longer than {MAX_ENTRY_LENGTH} characters."
        )

    if "://" in entry:
        return _parse_url_form(entry)
    if "@" in entry:
        return _parse_credentials_at_hostport(entry)
    return _parse_plain_form(entry)


def _parse_url_form(entry: str) -> ParsedProxy:
    """Parse ``scheme://[user:pass@]host[:port]``."""
    parts = urlsplit(entry)
    protocol = normalize_protocol(parts.scheme)

    # A path, query or fragment means this is a URL someone pasted, not a proxy endpoint.
    # Silently ignoring it would store a proxy whose address differs from what the operator
    # intended, which is worse than a clear rejection.
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise InvalidProxyFormatError(
            "The proxy entry has a path, query or fragment; a proxy URL is scheme://host:port only."
        )

    host = normalize_host(parts.hostname or "")

    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise InvalidProxyFormatError("The proxy port is invalid.") from exc
    port = normalize_port(explicit_port) if explicit_port is not None else DEFAULT_PORTS[protocol]

    # ``urlsplit`` yields an empty string (not None) for ``http://:pass@host``, so test for
    # ``None`` explicitly: an empty username is a malformed entry, not "no username".
    username = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None
    if username is not None and not username.strip():
        raise InvalidProxyFormatError("The proxy entry has an empty username.")

    return ParsedProxy(
        protocol=protocol,
        host=host,
        port=port,
        username=username.strip() if username else None,
        password=password or None,
    )


def _parse_credentials_at_hostport(entry: str) -> ParsedProxy:
    """Parse ``user:pass@host:port``."""
    credentials, _, hostport = entry.rpartition("@")
    username, password = split_credentials(credentials)
    host, port = _split_host_port(hostport)
    return ParsedProxy(
        protocol=ProxyProtocol.HTTP.value,
        host=host,
        port=port,
        username=username,
        password=password,
    )


def _parse_plain_form(entry: str) -> ParsedProxy:
    """Parse ``host:port`` or ``host:port:user:pass``.

    Field counts of exactly 3 are rejected rather than guessed: ``host:port:user`` could mean a
    username with no password, or a truncated four-field line, and silently picking one would
    store a proxy that behaves differently from what the operator expected.
    """
    # IPv6 in brackets may legitimately contain colons, so bracket it out before splitting.
    if entry.startswith("["):
        closing = entry.find("]")
        if closing == -1:
            raise InvalidProxyFormatError("The proxy entry has an unclosed IPv6 bracket.")
        host_raw = entry[: closing + 1]
        remainder = entry[closing + 1 :].lstrip(":")
        if not remainder:
            raise InvalidProxyFormatError("The proxy entry has no port.")
        fields = [host_raw, *remainder.split(":")]
    else:
        fields = entry.split(":")

    if len(fields) == 2:
        host, port = normalize_host(fields[0]), normalize_port(fields[1])
        return ParsedProxy(protocol=ProxyProtocol.HTTP.value, host=host, port=port)
    if len(fields) == 4:
        host = normalize_host(fields[0])
        port = normalize_port(fields[1])
        username = unquote(fields[2]).strip()
        if not username:
            raise InvalidProxyFormatError("The proxy entry has an empty username.")
        password = unquote(fields[3]) or None
        return ParsedProxy(
            protocol=ProxyProtocol.HTTP.value,
            host=host,
            port=port,
            username=username,
            password=password,
        )
    if len(fields) == 3:
        raise InvalidProxyFormatError(
            "Ambiguous proxy entry with 3 fields. Use host:port, host:port:user:pass, or a URL."
        )
    raise InvalidProxyFormatError(
        f"Unrecognized proxy entry shape ({len(fields)} fields). Expected host:port."
    )


def _split_host_port(hostport: str) -> tuple[str, int]:
    """Split ``host:port`` for the credentials form, handling bracketed IPv6."""
    text = hostport.strip()
    if text.startswith("["):
        closing = text.find("]")
        if closing == -1:
            raise InvalidProxyFormatError("The proxy entry has an unclosed IPv6 bracket.")
        host = normalize_host(text[: closing + 1])
        remainder = text[closing + 1 :].lstrip(":")
        if not remainder:
            raise InvalidProxyFormatError("The proxy entry has no port.")
        return host, normalize_port(remainder)
    host_raw, _, port_raw = text.rpartition(":")
    if not host_raw:
        raise InvalidProxyFormatError("The proxy entry has no host.")
    return normalize_host(host_raw), normalize_port(port_raw)

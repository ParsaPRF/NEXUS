"""Desktop client configuration.

Everything the client needs comes from the environment, and nothing it needs is a secret it has to
store: the API key is read from the environment like the backend's own secrets are. The desktop
application never touches the database and never imports the backend package - see D7 in
docs/DECISION_LOG.md - so this module is the entire surface where the two halves meet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Default API location. Loopback, matching the backend's own default bind address.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: Seconds before a request is abandoned. A frozen UI waiting on a socket is the failure this
#: application exists to avoid, so the timeout is deliberately short and always set.
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    """Where the API lives and how to authenticate."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT
    verify_tls: bool = True

    @classmethod
    def from_environment(cls) -> DesktopConfig:
        """Read configuration from ``NEXUS_DESKTOP_*`` variables.

        ``NEXUS_API_KEY`` is accepted as a fallback so a developer who already exported the
        backend's key does not have to set it twice.
        """
        return cls(
            base_url=os.environ.get("NEXUS_DESKTOP_API_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_key=os.environ.get("NEXUS_DESKTOP_API_KEY", os.environ.get("NEXUS_API_KEY", "")),
            timeout_seconds=float(os.environ.get("NEXUS_DESKTOP_TIMEOUT", str(DEFAULT_TIMEOUT))),
            verify_tls=os.environ.get("NEXUS_DESKTOP_VERIFY_TLS", "true").lower()
            not in {"0", "false", "no"},
        )

    @property
    def is_configured(self) -> bool:
        """True when a base URL is present (an API key may legitimately be empty in development)."""
        return bool(self.base_url)

"""Enumerations shared across models and API schemas.

Why enums instead of bare strings?
----------------------------------
A bare string column accepts any typo (``"helthy"``) silently. An enum makes the set of legal
values explicit, gives editors autocompletion, and lets tests iterate over every possible case.

These are ``StrEnum`` subclasses, so they compare equal to their string values. That matters for
two reasons: SQLAlchemy stores the plain string, and the JSON API emits ``"healthy"`` rather
than an integer, which keeps exported reports readable.
"""

from __future__ import annotations

from enum import StrEnum


class ProxyProtocol(StrEnum):
    """Supported proxy protocols."""

    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"


class ProxyStatus(StrEnum):
    """Health state of a stored proxy."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    EXPIRED = "expired"


class ProxySourceKind(StrEnum):
    """Where a proxy list came from."""

    MANUAL = "manual"
    FILE = "file"
    URL = "url"


class InboxStatus(StrEnum):
    """Lifecycle state of a disposable test inbox."""

    ACTIVE = "active"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class CampaignStatus(StrEnum):
    """Lifecycle state of a registration campaign."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED_BY_TARGET = "stopped_by_target"


class TestStatus(StrEnum):
    """Lifecycle state of a single registration test."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    AWAITING_EMAIL = "awaiting_email"
    VERIFIED = "verified"
    ACTIVE = "active"
    FAILED = "failed"
    BLOCKED_BY_TARGET = "blocked_by_target"


class ResultOutcome(StrEnum):
    """Analysed outcome of a registration test.

    The split between "product defect" and "control encountered" is deliberate
    (docs/DATABASE_DESIGN.md 3.11): a verification failure is a bug report, while a rate limit
    is a boundary NEXUS respects. Dashboards must never average the two together.
    """

    # --- product defects: something in the tested application did not work ---
    SUCCESS = "success"
    VERIFICATION_FAILED = "verification_failed"
    EMAIL_TIMEOUT = "email_timeout"
    VALIDATION_ERROR = "validation_error"
    UNEXPECTED_ERROR = "unexpected_error"

    # --- controls encountered: terminal by design, the run stops ---
    RATE_LIMITED = "rate_limited"
    CAPTCHA_ENCOUNTERED = "captcha_encountered"
    ACCESS_DENIED = "access_denied"

    @classmethod
    def terminal_control_outcomes(cls) -> frozenset[ResultOutcome]:
        """Outcomes that mean "stop the campaign", not "retry"."""
        return frozenset({cls.RATE_LIMITED, cls.CAPTCHA_ENCOUNTERED, cls.ACCESS_DENIED})

    @property
    def is_control_encountered(self) -> bool:
        """True when the target refused to cooperate because of an anti-abuse control."""
        return self in self.terminal_control_outcomes()


class JobStatus(StrEnum):
    """Lifecycle state of a background job."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True when no further transition is possible."""
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class JobLogLevel(StrEnum):
    """Severity of a job log line."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AuditOutcome(StrEnum):
    """Result of an audited action."""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class TargetKind(StrEnum):
    """How a registration target is driven."""

    DEMO = "demo"
    API = "api"
    UI = "ui"

"""Domain exception hierarchy.

Why a hierarchy instead of raising ``HTTPException`` directly?
-------------------------------------------------------------
The service layer must not know about HTTP. If a service raised ``HTTPException`` it would
import FastAPI, and the dependency arrows in docs/ARCHITECTURE.md 6 would break - the same
service could no longer be reused by a background worker or a CLI.

So: services raise **domain errors**, and a single exception handler in ``app/main.py`` maps
each class to exactly one HTTP status code. Adding a new failure mode means adding one exception
class plus one mapping entry, never an ad-hoc status code in a route.

Every error also carries a stable machine-readable ``code`` so clients and tests can assert on
it without string-matching prose.
"""

from __future__ import annotations

from typing import Any


class NexusError(Exception):
    """Base class for every error NEXUS raises deliberately.

    Attributes:
        code: Stable identifier, e.g. ``"ssrf_blocked"``. Part of the API contract.
        http_status: The status code the API layer should return.
        message: Safe, human-readable description. Never contains secrets.
        details: Optional structured context included in the error body.
    """

    code: str = "internal_error"
    http_status: int = 500
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details: dict[str, Any] = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the HTTP response body."""
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


# --------------------------------------------------------------------------------------
# 400 / 422 - the caller sent something wrong
# --------------------------------------------------------------------------------------


class ValidationError(NexusError):
    """Input failed validation. Distinct from pydantic's own ``ValidationError``."""

    code = "validation_error"
    http_status = 422


class InvalidProxyFormatError(ValidationError):
    """A proxy string could not be parsed into protocol/host/port."""

    code = "invalid_proxy_format"
    message = "The proxy entry could not be parsed."


class UnsupportedProtocolError(ValidationError):
    """The proxy scheme is not one of http/https/socks5."""

    code = "unsupported_protocol"
    message = "Unsupported proxy protocol."


class ImportFileError(ValidationError):
    """An imported file is unreadable, malformed, or too large."""

    code = "import_file_error"
    message = "The import file could not be processed."


# --------------------------------------------------------------------------------------
# 403 - understood, but refused by policy (these are the safety controls)
# --------------------------------------------------------------------------------------


class PolicyViolationError(NexusError):
    """Base class for refusals based on the project's safety rules."""

    code = "policy_violation"
    http_status = 403
    message = "This operation is not permitted."


class SsrfBlockedError(PolicyViolationError):
    """A user-supplied URL points at a blocked address range or scheme."""

    code = "ssrf_blocked"
    message = "The requested URL is not permitted (private, loopback, metadata or non-HTTP)."


class TargetNotAuthorizedError(PolicyViolationError):
    """The registration target is not (or not yet) explicitly authorized."""

    code = "target_not_authorized"
    message = (
        "Registration may only run against a target that is explicitly authorized "
        "and present in the allowlist."
    )


class CredentialsNotStorableError(PolicyViolationError):
    """Proxy credentials were supplied but no encryption key is configured.

    We refuse rather than store a credential in plain text (docs/SECURITY.md T5).
    """

    code = "credentials_not_storable"
    message = (
        "Proxy credentials cannot be stored because NEXUS_PROXY_CREDENTIAL_KEY is not "
        "configured. Set the key or import the proxy without credentials."
    )


class EmailDomainNotAllowedError(PolicyViolationError):
    """A mail domain is neither RFC-reserved nor explicitly owner-verified.

    This is a policy refusal, not a syntax error: the domain may be perfectly well formed, but
    NEXUS will not host test inboxes on a domain the operator has not shown they control
    (docs/SECURITY.md 3).
    """

    code = "email_domain_not_allowed"
    message = (
        "Only RFC-reserved test domains (example.test and friends) or explicitly "
        "owner-verified domains may be used for test inboxes."
    )


class TargetControlEncounteredError(PolicyViolationError):
    """The target answered with an anti-abuse control (429, CAPTCHA, denial).

    This is deliberately an error *and* deliberately terminal: NEXUS reports it and stops
    instead of retrying or working around it (docs/SECURITY.md T12, Decision D8).
    """

    code = "target_control_encountered"
    message = (
        "The target responded with a rate limit or anti-automation control. The run was stopped."
    )


# --------------------------------------------------------------------------------------
# 404 / 409 - state problems
# --------------------------------------------------------------------------------------


class NotFoundError(NexusError):
    """The requested entity does not exist."""

    code = "not_found"
    http_status = 404
    message = "The requested resource was not found."


class ConflictError(NexusError):
    """The request conflicts with existing state (duplicate, wrong status)."""

    code = "conflict"
    http_status = 409


class JobCancelledError(NexusError):
    """A background job was cancelled cooperatively."""

    code = "job_cancelled"
    http_status = 409
    message = "The job was cancelled."


# --------------------------------------------------------------------------------------
# 401 / 403 - authentication and authorization
# --------------------------------------------------------------------------------------


class AuthenticationError(NexusError):
    """Missing or invalid credentials."""

    code = "unauthenticated"
    http_status = 401
    message = "A valid API key is required."


class AuthorizationError(NexusError):
    """Authenticated, but the role is insufficient."""

    code = "forbidden"
    http_status = 403
    message = "Your role does not permit this operation."


# --------------------------------------------------------------------------------------
# 429 / 500 / 503 - capacity and unexpected failures
# --------------------------------------------------------------------------------------


class RateLimitedError(NexusError):
    """NEXUS's own inbound rate limit was exceeded."""

    code = "rate_limited"
    http_status = 429
    message = "Too many requests. Please slow down."


class ExternalServiceError(NexusError):
    """A dependency (mail backend, database, queue) is unavailable or misbehaved."""

    code = "external_service_error"
    http_status = 503
    message = "A required external service is unavailable."


class ConfigurationError(NexusError):
    """The application is misconfigured."""

    code = "configuration_error"
    http_status = 500
    message = "The application is misconfigured."

"""API schemas for the Email Manager.

``body_html`` is returned only in the message-detail schema, and it is documented as untrusted.
The desktop UI must render it as plain text - an email body is attacker-controlled input and
rendering it as HTML is how a QA tool becomes an XSS vector (threat T10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.common import ApiModel, Page, PageMeta

#: Export formats supported by the message export endpoint.
MessageExportFormat = Literal["csv", "json"]


class EmailDomainRead(ApiModel):
    """A domain that test inboxes may be created on."""

    id: int
    domain: str
    is_reserved: bool = Field(
        description="True for RFC 2606/6761 reserved test domains, which cannot receive real mail."
    )
    owner_verified: bool = Field(
        description="Set only when the operator confirmed they control the domain."
    )
    is_active: bool
    description: str | None = None
    inbox_count: int = Field(default=0, description="Inboxes currently hosted on this domain.")


class EmailDomainCreate(ApiModel):
    """Register a domain for test inboxes.

    A non-reserved domain requires ``owner_verified=true``: NEXUS will not host test inboxes on a
    domain the operator may not control.
    """

    domain: str = Field(min_length=3, max_length=255)
    owner_verified: bool = Field(
        default=False,
        description="Confirm you control this domain. Required for non-reserved TLDs.",
    )
    description: str | None = Field(default=None, max_length=255)

    @field_validator("domain")
    @classmethod
    def normalize(cls, value: str) -> str:
        """Lowercase and strip a trailing dot, then verify the shape."""
        domain = value.strip().lower().rstrip(".")
        if "." not in domain or " " in domain:
            msg = "A domain must look like example.test"
            raise ValueError(msg)
        return domain


class InboxRead(ApiModel):
    """A disposable test inbox."""

    public_id: str
    address: str
    local_part: str
    domain: str
    label: str | None
    status: str = Field(description="active | expired | archived")
    message_count: int
    last_message_at: datetime | None
    expires_at: datetime | None
    campaign_id: int | None
    created_at: datetime


class InboxCreate(ApiModel):
    """Create one inbox."""

    label: str | None = Field(default=None, max_length=128)
    ttl_hours: Annotated[int, Field(ge=1, le=8760)] | None = Field(
        default=None, description="Lifetime in hours. Defaults to the configured TTL."
    )
    local_part: str | None = Field(
        default=None,
        max_length=64,
        description="Request a specific alias instead of a random one. Must be unique.",
    )

    @field_validator("local_part")
    @classmethod
    def validate_local_part(cls, value: str | None) -> str | None:
        """Keep aliases to characters that are safe in an address."""
        if value is None:
            return None
        alias = value.strip().lower()
        if not alias:
            return None
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")
        if not set(alias) <= allowed:
            msg = "An alias may contain only letters, digits, dot, dash and underscore"
            raise ValueError(msg)
        if alias.startswith(".") or alias.endswith(".") or ".." in alias:
            msg = "An alias may not start or end with a dot, or contain two dots"
            raise ValueError(msg)
        return alias


class InboxBulkCreate(ApiModel):
    """Create several inboxes at once."""

    count: Annotated[int, Field(ge=1, le=500)] = Field(
        description="How many inboxes to create, capped by configuration."
    )
    label_prefix: str | None = Field(default=None, max_length=96)
    ttl_hours: Annotated[int, Field(ge=1, le=8760)] | None = None


class InboxBulkResponse(ApiModel):
    """The created inboxes."""

    created: int
    inboxes: list[InboxRead]


class InboxListResponse(ApiModel):
    """A page of inboxes."""

    items: list[InboxRead]
    meta: PageMeta


class InboxPollResponse(ApiModel):
    """Result of ingesting messages for one inbox."""

    inbox: str = Field(description="Inbox address that was polled.")
    ingested: int = Field(description="New messages stored.")
    skipped: int = Field(description="Messages ignored (wrong recipient, oversized, unreadable).")
    already_known: int = Field(description="Messages that were already stored.")


class MessageSummary(ApiModel):
    """A message without its body, for list views."""

    id: int
    inbox_id: int
    sender: str
    recipient: str
    subject: str | None
    received_at: datetime
    size_bytes: int | None
    extracted_code: str | None
    extraction_confidence: float | None


class MessageRead(MessageSummary):
    """A full message, including bodies."""

    body_text: str | None
    body_html: str | None = Field(
        default=None,
        description=(
            "Untrusted HTML. Stored for completeness and extraction only; a client must not "
            "render it as markup."
        ),
    )
    headers: dict[str, str] | None = None


class MessageListResponse(ApiModel):
    """A page of messages."""

    items: list[MessageSummary]
    meta: PageMeta


class CodeExtractionRead(ApiModel):
    """The outcome of verification-code extraction for one message."""

    message_id: int
    code: str | None
    confidence: float
    strategy: str | None
    candidates: list[dict[str, object]] = Field(
        default_factory=list, description="Ranked candidates, best first."
    )


class InboxCleanupResponse(ApiModel):
    """Result of an expiry sweep."""

    expired: int = Field(description="Inboxes moved from active to expired.")


class InboxExportResponse(ApiModel):
    """Metadata about an export (the file itself is streamed)."""

    inbox: str
    format: str
    message_count: int


#: Page of inboxes, expressed with the shared generic.
PageOfInboxes = Page[InboxRead]

#: Columns written to a message export. Body text is included but capped.
MESSAGE_EXPORT_FIELDS: tuple[str, ...] = (
    "id",
    "sender",
    "recipient",
    "subject",
    "received_at",
    "extracted_code",
    "extraction_confidence",
    "body_text_preview",
)

#: Maximum characters of a body included in an export row.
BODY_PREVIEW_CHARS = 2000

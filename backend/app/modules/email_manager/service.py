"""Email Manager service - disposable test inboxes and message ingestion.

Rules enforced here:

* **Only reserved or owner-verified domains host inboxes.** A non-reserved domain requires an
  explicit ownership confirmation; the default (``example.test``) is RFC-reserved, so it can
  never receive real mail.
* **Ingestion is idempotent.** Polling twice cannot duplicate a message: known backend ids are
  skipped, and the database's unique constraint is the final backstop.
* **A message is only stored for the inbox it was addressed to.** Even if a backend returns a
  message for someone else, it is rejected - defence in depth against a confused or hostile
  backend.
* **Bodies are capped** and HTML is stored, never rendered.
* Expiry is a bulk status change, not a deletion: the history stays inspectable until the
  operator deletes the inbox.
"""

from __future__ import annotations

import csv
import io
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    EmailDomainNotAllowedError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.email_models import EmailDomain, Inbox, Message
from app.models.enums import InboxStatus
from app.modules.email_manager.backends import InboundMessage, MailBackend, get_backend
from app.modules.email_manager.extraction import ExtractionResult, extract_verification_code
from app.modules.email_manager.repository import EmailRepository, InboxQuery
from app.modules.email_manager.schemas import (
    BODY_PREVIEW_CHARS,
    MESSAGE_EXPORT_FIELDS,
    CodeExtractionRead,
    EmailDomainRead,
    InboxBulkResponse,
    InboxPollResponse,
    InboxRead,
    MessageRead,
    MessageSummary,
)
from app.services.audit_service import AuditService

logger = get_logger(__name__)

#: RFC 2606/6761 reserved top-level domains. Mail sent to these can never leave the machine.
RESERVED_TLDS: frozenset[str] = frozenset({"test", "invalid", "example", "localhost"})

#: How many times a random alias is retried on collision.
_ALIAS_ATTEMPTS = 5

#: Hard ceiling on an export.
MAX_EXPORT_MESSAGES = 5_000


def is_reserved_domain(domain: str) -> bool:
    """True when ``domain`` sits under an RFC-reserved TLD."""
    return domain.strip().lower().rstrip(".").rsplit(".", 1)[-1] in RESERVED_TLDS


def _random_alias() -> str:
    """Return a random, readable local part such as ``qa-3f9a2b7c1d4e``."""
    return f"qa-{secrets.token_hex(6)}"


class InboxService:
    """Use cases for domains, inboxes and messages."""

    def __init__(
        self,
        session: Any,
        settings: Settings,
        *,
        backend: MailBackend | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._repo = EmailRepository(session)
        self._backend = backend or get_backend(settings)
        self._audit = AuditService(session, enabled=settings.audit_log_enabled)

    # ------------------------------------------------------------------ domains

    async def list_domains(self) -> list[EmailDomainRead]:
        """Return configured domains with their inbox counts."""
        domains = await self._repo.list_domains()
        result: list[EmailDomainRead] = []
        for domain in domains:
            result.append(
                EmailDomainRead(
                    id=domain.id,
                    domain=domain.domain,
                    is_reserved=bool(domain.is_reserved),
                    owner_verified=bool(domain.owner_verified),
                    is_active=bool(domain.is_active),
                    description=domain.description,
                    inbox_count=await self._repo.inbox_count_for_domain(domain.id),
                )
            )
        return result

    async def create_domain(
        self,
        domain: str,
        *,
        owner_verified: bool = False,
        description: str | None = None,
        request: Any | None = None,
    ) -> EmailDomainRead:
        """Register a domain for test inboxes.

        Raises:
            EmailDomainNotAllowedError: the domain is not RFC-reserved and ownership was not
                explicitly confirmed.
            ConflictError: the domain is already registered.
        """
        normalized = domain.strip().lower().rstrip(".")
        reserved = is_reserved_domain(normalized)
        if not reserved and not owner_verified:
            raise EmailDomainNotAllowedError(
                f"{normalized!r} is not an RFC-reserved test domain. Confirm ownership with "
                "owner_verified=true only if you control it.",
                details={"domain": normalized, "reserved_tlds": sorted(RESERVED_TLDS)},
            )
        if await self._repo.get_domain_by_name(normalized) is not None:
            raise ConflictError(f"Domain {normalized!r} is already registered.")

        created = await self._repo.add_domain(
            EmailDomain(
                domain=normalized,
                is_reserved=reserved,
                owner_verified=bool(owner_verified),
                is_active=True,
                description=description,
            )
        )
        await self._audit.record(
            "domain.create",
            entity_type="email_domain",
            entity_id=created.id,
            detail={"domain": normalized, "reserved": reserved, "owner_verified": owner_verified},
            request_id=_request_id(request),
        )
        await self._session.commit()
        return EmailDomainRead(
            id=created.id,
            domain=created.domain,
            is_reserved=bool(created.is_reserved),
            owner_verified=bool(created.owner_verified),
            is_active=bool(created.is_active),
            description=created.description,
            inbox_count=0,
        )

    async def _resolve_domain(self, domain_name: str | None) -> EmailDomain:
        """Return the domain to host a new inbox on.

        Raises:
            EmailDomainNotAllowedError: the named domain is unknown or not usable.
        """
        if not domain_name:
            return await self._default_domain()

        normalized = domain_name.strip().lower().rstrip(".")
        domain = await self._repo.get_domain_by_name(normalized)
        if domain is None or not domain.is_usable:
            raise EmailDomainNotAllowedError(
                f"Domain {normalized!r} is not registered and verified for test inboxes.",
                details={"domain": normalized},
            )
        return domain

    async def _default_domain(self) -> EmailDomain:
        """Return the configured default domain, creating it when it is reserved.

        Raises:
            EmailDomainNotAllowedError: the configured domain exists but is not usable, or is not
                reserved and therefore cannot be auto-created.
        """
        configured = self._settings.mail.reserved_domain.strip().lower().rstrip(".")
        existing = await self._repo.get_domain_by_name(configured)
        if existing is not None:
            if not existing.is_usable:
                raise EmailDomainNotAllowedError(
                    f"The configured inbox domain {configured!r} is not active and verified.",
                    details={"domain": configured},
                )
            return existing

        if not is_reserved_domain(configured):
            raise EmailDomainNotAllowedError(
                f"The configured inbox domain {configured!r} is not RFC-reserved. Register it "
                "with POST /api/v1/email-domains and owner_verified=true after confirming you "
                "control it.",
                details={"domain": configured},
            )
        return await self._repo.add_domain(
            EmailDomain(
                domain=configured,
                is_reserved=True,
                owner_verified=False,
                is_active=True,
                description="Default RFC-reserved test domain",
            )
        )

    # ------------------------------------------------------------------ inboxes

    async def create_inbox(
        self,
        *,
        label: str | None = None,
        ttl_hours: int | None = None,
        local_part: str | None = None,
        campaign_id: int | None = None,
        domain_name: str | None = None,
        request: Any | None = None,
    ) -> InboxRead:
        """Create one inbox with a random (or requested) alias.

        ``domain_name`` selects a specific hosted domain; when omitted the configured default is
        used. The named domain must be usable (active and reserved or owner-verified).

        Raises:
            ConflictError: the requested alias is already taken.
            EmailDomainNotAllowedError: the requested or default domain cannot host inboxes.
        """
        now = datetime.now(UTC)
        domain = await self._resolve_domain(domain_name)
        alias = local_part or await self._unique_alias(domain)
        address = f"{alias}@{domain.domain}"

        if await self._repo.address_exists(address):
            raise ConflictError(
                f"The address {address!r} is already in use.", details={"address": address}
            )

        inbox = await self._repo.add_inbox(
            Inbox(
                public_id=str(uuid.uuid4()),
                domain_id=domain.id,
                local_part=alias,
                address=address,
                label=label,
                status=InboxStatus.ACTIVE.value,
                expires_at=self._expiry(ttl_hours),
                campaign_id=campaign_id,
                created_at=now,
                updated_at=now,
            )
        )
        await self._audit.record(
            "inbox.create",
            entity_type="inbox",
            entity_id=inbox.id,
            detail={"address": address, "label": label},
            request_id=_request_id(request),
        )
        await self._session.commit()
        return _to_inbox_read(inbox, domain.domain)

    async def bulk_create_inboxes(
        self,
        *,
        count: int,
        label_prefix: str | None = None,
        ttl_hours: int | None = None,
        request: Any | None = None,
    ) -> InboxBulkResponse:
        """Create several inboxes in one transaction.

        Raises:
            ValidationError: ``count`` exceeds ``NEXUS_MAIL__MAX_BULK_INBOXES``.
        """
        limit = self._settings.mail.max_bulk_inboxes
        if count > limit:
            raise ValidationError(
                f"A bulk request may create at most {limit} inboxes.",
                details={"requested": count, "limit": limit},
            )

        now = datetime.now(UTC)
        domain = await self._default_domain()
        created: list[InboxRead] = []
        for index in range(count):
            alias = await self._unique_alias(domain)
            address = f"{alias}@{domain.domain}"
            label = f"{label_prefix}-{index + 1}" if label_prefix else None
            inbox = await self._repo.add_inbox(
                Inbox(
                    public_id=str(uuid.uuid4()),
                    domain_id=domain.id,
                    local_part=alias,
                    address=address,
                    label=label,
                    status=InboxStatus.ACTIVE.value,
                    expires_at=self._expiry(ttl_hours),
                    created_at=now,
                    updated_at=now,
                )
            )
            created.append(_to_inbox_read(inbox, domain.domain))

        await self._audit.record(
            "inbox.bulk_create",
            entity_type="inbox",
            entity_id=domain.id,
            detail={"count": count, "domain": domain.domain, "label_prefix": label_prefix},
            request_id=_request_id(request),
        )
        await self._session.commit()
        return InboxBulkResponse(created=len(created), inboxes=created)

    async def _unique_alias(self, domain: EmailDomain) -> str:
        """Generate a random alias that is not taken on ``domain``."""
        for _ in range(_ALIAS_ATTEMPTS):
            alias = _random_alias()
            if not await self._repo.address_exists(f"{alias}@{domain.domain}"):
                return alias
        raise ConflictError("Could not allocate a unique inbox alias; please retry.")

    def _expiry(self, ttl_hours: int | None) -> datetime:
        """Return the expiry timestamp for a new inbox."""
        hours = ttl_hours if ttl_hours is not None else self._settings.mail.default_ttl_hours
        return datetime.now(UTC) + timedelta(hours=hours)

    async def list_inboxes(self, query: InboxQuery) -> tuple[list[InboxRead], int]:
        """Return a page of inboxes."""
        rows, total = await self._repo.list_inboxes(query)
        names = await self._domain_names()
        return [_to_inbox_read(inbox, names.get(inbox.domain_id, "")) for inbox in rows], total

    async def get_inbox(self, public_id: str) -> Inbox:
        """Return one inbox by its public id.

        Raises:
            NotFoundError: no inbox has that public id.
        """
        inbox = await self._repo.get_inbox_by_public_id(public_id)
        if inbox is None:
            raise NotFoundError(
                f"Inbox {public_id!r} was not found.", details={"public_id": public_id}
            )
        return inbox

    async def ensure_inbox_for_address(
        self, address: str, *, label: str | None = None
    ) -> InboxRead:
        """Return the inbox for ``address``, creating it when it does not exist.

        Used by imported campaign identities: reusing an existing inbox matters, because creating
        a second one for the same address would conflict (and would split the mail across two
        rows).

        Raises:
            EmailDomainNotAllowedError: the address's domain is not hosted or not usable.
            ValidationError: the address is malformed.
        """
        normalized = address.strip().lower()
        existing = await self._repo.get_inbox_by_address(normalized)
        if existing is not None:
            names = await self._domain_names()
            return _to_inbox_read(existing, names.get(existing.domain_id, ""))
        local_part, _, domain_name = normalized.partition("@")
        if not local_part or not domain_name:
            raise ValidationError(
                f"{address!r} is not a valid address.", details={"address": address}
            )
        return await self.create_inbox(local_part=local_part, domain_name=domain_name, label=label)

    async def get_inbox_by_id(self, inbox_id: int) -> Inbox:
        """Return one inbox by primary key.

        Used by the registration runner, which stores the numeric id on a test row.

        Raises:
            NotFoundError: no inbox has that id.
        """
        if inbox_id is None:
            raise NotFoundError("The test has no inbox attached.")
        inbox = await self._repo.get_inbox(inbox_id)
        if inbox is None:
            raise NotFoundError(f"Inbox {inbox_id} was not found.", details={"inbox_id": inbox_id})
        return inbox

    async def get_inbox_read(self, public_id: str) -> InboxRead:
        """Return one inbox as a read schema."""
        inbox = await self.get_inbox(public_id)
        names = await self._domain_names()
        return _to_inbox_read(inbox, names.get(inbox.domain_id, ""))

    async def delete_inbox(self, public_id: str, *, request: Any | None = None) -> None:
        """Delete an inbox and its messages.

        Raises:
            NotFoundError: no inbox has that public id.
        """
        inbox = await self.get_inbox(public_id)
        address = inbox.address
        await self._repo.delete_inbox(inbox)
        await self._audit.record(
            "inbox.delete",
            entity_type="inbox",
            entity_id=public_id,
            detail={"address": address},
            request_id=_request_id(request),
        )
        await self._session.commit()

    async def _domain_names(self) -> dict[int, str]:
        """Return a ``domain_id -> domain`` map (there are only a handful of domains)."""
        return {domain.id: domain.domain for domain in await self._repo.list_domains()}

    # ------------------------------------------------------------------ ingestion

    async def poll_inbox(self, public_id: str, *, request: Any | None = None) -> InboxPollResponse:
        """Fetch new messages for one inbox from the mail backend.

        Raises:
            NotFoundError: no inbox has that public id.
            ExternalServiceError: the mail backend is unreachable.
        """
        inbox = await self.get_inbox(public_id)
        ingested, skipped, already_known = await self._ingest(inbox)
        if ingested:
            await self._audit.record(
                "inbox.poll",
                entity_type="inbox",
                entity_id=inbox.id,
                detail={"ingested": ingested, "skipped": skipped},
                request_id=_request_id(request),
            )
        await self._session.commit()
        return InboxPollResponse(
            inbox=inbox.address, ingested=ingested, skipped=skipped, already_known=already_known
        )

    async def _ingest(self, inbox: Inbox) -> tuple[int, int, int]:
        """Store new messages for ``inbox``. Returns ``(ingested, skipped, already_known)``."""
        max_messages = self._settings.mail.max_messages_per_poll
        try:
            backend_ids = await self._backend.list_ids(inbox.address, limit=max_messages)
        except ExternalServiceError:
            raise
        known = await self._repo.known_backend_ids(inbox.id)
        new_ids = [backend_id for backend_id in backend_ids if backend_id not in known][
            :max_messages
        ]

        ingested = 0
        skipped = 0
        received_times: list[datetime] = []
        for backend_id in new_ids:
            message = await self._backend.fetch(backend_id)
            if message is None:
                skipped += 1
                continue
            if not self._was_addressed_to(message, inbox):
                # Defence in depth: never store mail that belongs to another inbox.
                logger.warning(
                    "Skipping message %s: addressed to %s, not %s",
                    backend_id,
                    message.recipient,
                    inbox.address,
                    extra={"module_name": "email_manager"},
                )
                skipped += 1
                continue
            self._store(inbox, message)
            received_times.append(message.received_at)
            ingested += 1

        if ingested:
            inbox.message_count = (inbox.message_count or 0) + ingested
            # "Last message" means when mail arrived, not when the poll ran: an empty poll must
            # not look like activity, and a poll of old mail must not pretend to be fresh.
            inbox.last_message_at = max(received_times)
            await self._session.flush()
        return ingested, skipped, len(known & set(backend_ids))

    def _was_addressed_to(self, message: InboundMessage, inbox: Inbox) -> bool:
        """True when the message is addressed to this inbox."""
        return message.recipient.strip().lower() == inbox.address.strip().lower()

    def _store(self, inbox: Inbox, message: InboundMessage) -> None:
        """Add a Message row for ``message`` under ``inbox``."""
        extraction: ExtractionResult = extract_verification_code(
            text=message.body_text, html=message.body_html
        )
        self._session.add(
            Message(
                inbox_id=inbox.id,
                backend_message_id=message.backend_id,
                sender=message.sender[:320] or "unknown@unknown.invalid",
                recipient=message.recipient[:320],
                subject=(message.subject or "")[:512] or None,
                body_text=message.body_text,
                body_html=message.body_html,
                received_at=message.received_at,
                size_bytes=message.size_bytes,
                extracted_code=extraction.code,
                extraction_confidence=extraction.confidence,
                headers_json=dict(message.headers) or None,
            )
        )

    async def poll_all_active(self, *, limit: int = 100) -> dict[str, int]:
        """Poll every active inbox. Used by the scheduled job.

        A single unreachable inbox must not stop the sweep, so failures are counted and logged.
        """
        inboxes = await self._repo.list_active_inboxes(limit=limit)
        total_ingested = 0
        failures = 0
        for inbox in inboxes:
            try:
                ingested, _skipped, _known = await self._ingest(inbox)
                total_ingested += ingested
            except ExternalServiceError:
                failures += 1
                logger.warning(
                    "Polling %s failed", inbox.address, extra={"module_name": "email_manager"}
                )
        await self._session.commit()
        return {
            "inboxes": len(inboxes),
            "ingested": total_ingested,
            "failures": failures,
        }

    # ------------------------------------------------------------------ messages

    async def list_messages(
        self, public_id: str, *, search: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[MessageSummary], int]:
        """Return one page of an inbox's messages.

        Raises:
            NotFoundError: no inbox has that public id.
        """
        inbox = await self.get_inbox(public_id)
        rows, total = await self._repo.list_messages(
            inbox.id, search=search, limit=limit, offset=offset
        )
        return [_to_summary(row) for row in rows], total

    async def get_message(self, message_id: int) -> MessageRead:
        """Return one message, including bodies.

        Raises:
            NotFoundError: no message has that id.
        """
        message = await self._repo.get_message(message_id)
        if message is None:
            raise NotFoundError(
                f"Message {message_id} was not found.", details={"message_id": message_id}
            )
        return _to_message_read(message)

    async def extract_code(self, message_id: int) -> CodeExtractionRead:
        """Re-run extraction for one message and cache the result.

        Recomputing is useful when extraction improves: the stored value is refreshed rather than
        leaving older messages permanently unevaluated.

        Raises:
            NotFoundError: no message has that id.
        """
        message = await self._repo.get_message(message_id)
        if message is None:
            raise NotFoundError(
                f"Message {message_id} was not found.", details={"message_id": message_id}
            )
        extraction = extract_verification_code(text=message.body_text, html=message.body_html)
        message.extracted_code = extraction.code
        message.extraction_confidence = extraction.confidence
        await self._session.commit()
        return CodeExtractionRead(
            message_id=message.id,
            code=extraction.code,
            confidence=extraction.confidence,
            strategy=extraction.strategy,
            candidates=[
                {"code": item.code, "confidence": item.confidence, "strategy": item.strategy}
                for item in extraction.candidates[:10]
            ],
        )

    async def export_messages(
        self, public_id: str, fmt: str, *, limit: int = 1000
    ) -> tuple[str, str]:
        """Export an inbox's messages as CSV or JSON.

        Raises:
            NotFoundError: no inbox has that public id.
            ValidationError: unsupported format or too many rows requested.
        """
        resolved = (fmt or "csv").lower()
        if resolved not in {"csv", "json"}:
            raise ValidationError(
                f"Unsupported export format {fmt!r}.", details={"allowed": ["csv", "json"]}
            )
        if limit > MAX_EXPORT_MESSAGES:
            raise ValidationError(f"An export may contain at most {MAX_EXPORT_MESSAGES} messages.")

        inbox = await self.get_inbox(public_id)
        rows = await self._repo.messages_for_export(inbox.id, limit)
        records = [_export_row(message) for message in rows]

        if resolved == "json":
            return json.dumps(records, indent=2, ensure_ascii=False), "application/json"

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(MESSAGE_EXPORT_FIELDS))
        writer.writeheader()
        writer.writerows(records)
        return buffer.getvalue(), "text/csv"

    # ------------------------------------------------------------------ expiry

    async def cleanup_expired(self, *, request: Any | None = None) -> int:
        """Mark overdue inboxes as expired. Returns how many changed."""
        expired = await self._repo.mark_expired()
        await self._audit.record(
            "inbox.cleanup",
            entity_type="inbox",
            detail={"expired": expired},
            request_id=_request_id(request),
        )
        await self._session.commit()
        return expired


# --------------------------------------------------------------------------------------
# Mapping helpers
# --------------------------------------------------------------------------------------


def _request_id(request: Any | None) -> str | None:
    """Read the request id from a request-like object, if present."""
    return getattr(getattr(request, "state", None), "request_id", None)


def _to_inbox_read(inbox: Inbox, domain_name: str) -> InboxRead:
    """Map an Inbox row to its API schema."""
    return InboxRead(
        public_id=inbox.public_id,
        address=inbox.address,
        local_part=inbox.local_part,
        domain=domain_name or inbox.address.rsplit("@", 1)[-1],
        label=inbox.label,
        status=inbox.status,
        message_count=inbox.message_count or 0,
        last_message_at=inbox.last_message_at,
        expires_at=inbox.expires_at,
        campaign_id=inbox.campaign_id,
        created_at=inbox.created_at,
    )


def _to_summary(message: Message) -> MessageSummary:
    """Map a Message row to the list schema (no bodies)."""
    return MessageSummary(
        id=message.id,
        inbox_id=message.inbox_id,
        sender=message.sender,
        recipient=message.recipient,
        subject=message.subject,
        received_at=message.received_at,
        size_bytes=message.size_bytes,
        extracted_code=message.extracted_code,
        extraction_confidence=message.extraction_confidence,
    )


def _to_message_read(message: Message) -> MessageRead:
    """Map a Message row to the detail schema."""
    return MessageRead(
        **_to_summary(message).model_dump(),
        body_text=message.body_text,
        body_html=message.body_html,
        headers=message.headers_json,
    )


def _export_row(message: Message) -> dict[str, object]:
    """Flatten a message into one export row."""
    body = (message.body_text or "").strip()
    return {
        "id": message.id,
        "sender": message.sender,
        "recipient": message.recipient,
        "subject": message.subject or "",
        "received_at": message.received_at.isoformat() if message.received_at else "",
        "extracted_code": message.extracted_code or "",
        "extraction_confidence": message.extraction_confidence
        if message.extraction_confidence is not None
        else "",
        "body_text_preview": body[:BODY_PREVIEW_CHARS],
    }


__all__ = [
    "MAX_EXPORT_MESSAGES",
    "RESERVED_TLDS",
    "InboxService",
    "is_reserved_domain",
]

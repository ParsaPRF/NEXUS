"""Data access for the Email Manager.

Queries only; rules live in the service. Two details worth noting:

* ``known_backend_ids`` exists so ingestion can skip messages it already has **before** inserting
  them. The unique constraint is still the final guarantee, but pre-filtering keeps a poll from
  provoking an integrity error that would poison the surrounding transaction.
* ``mark_expired`` is a bulk ``UPDATE``: an expiry sweep must not load thousands of rows into
  memory just to set a status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_models import EmailDomain, Inbox, Message
from app.models.enums import InboxStatus


@dataclass(slots=True)
class InboxQuery:
    """Filter, sort and paginate options for an inbox list."""

    statuses: list[str] = field(default_factory=list)
    campaign_id: int | None = None
    search: str | None = None
    expiring_only: bool = False
    limit: int = 50
    offset: int = 0


class EmailRepository:
    """Queries for domains, inboxes and messages."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ domains

    async def get_domain_by_name(self, domain: str) -> EmailDomain | None:
        """Return the domain row for ``domain``, case-insensitively."""
        result = await self._session.execute(
            select(EmailDomain).where(func.lower(EmailDomain.domain) == domain.lower())
        )
        return result.scalar_one_or_none()

    async def list_domains(self) -> list[EmailDomain]:
        """Return every configured domain, oldest first."""
        result = await self._session.execute(select(EmailDomain).order_by(EmailDomain.id))
        return list(result.scalars().all())

    async def add_domain(self, domain: EmailDomain) -> EmailDomain:
        """Persist a new domain."""
        self._session.add(domain)
        await self._session.flush()
        return domain

    async def inbox_count_for_domain(self, domain_id: int) -> int:
        """Count inboxes hosted on a domain."""
        return int(
            (
                await self._session.execute(
                    select(func.count()).select_from(Inbox).where(Inbox.domain_id == domain_id)
                )
            ).scalar_one()
        )

    # ------------------------------------------------------------------ inboxes

    async def add_inbox(self, inbox: Inbox) -> Inbox:
        """Persist a new inbox."""
        self._session.add(inbox)
        await self._session.flush()
        return inbox

    async def get_inbox(self, inbox_id: int) -> Inbox | None:
        """Return one inbox by primary key."""
        return await self._session.get(Inbox, inbox_id)

    async def get_inbox_by_public_id(self, public_id: str) -> Inbox | None:
        """Return one inbox by its public (API) identifier."""
        result = await self._session.execute(select(Inbox).where(Inbox.public_id == public_id))
        return result.scalar_one_or_none()

    async def get_inbox_by_address(self, address: str) -> Inbox | None:
        """Return one inbox by full address."""
        result = await self._session.execute(
            select(Inbox).where(func.lower(Inbox.address) == address.lower())
        )
        return result.scalar_one_or_none()

    async def address_exists(self, address: str) -> bool:
        """True when the address is already taken."""
        return await self.get_inbox_by_address(address) is not None

    async def list_inboxes(self, query: InboxQuery) -> tuple[list[Inbox], int]:
        """Return one page of inboxes and the total matching count."""
        statement = select(Inbox)
        if query.statuses:
            statement = statement.where(Inbox.status.in_(query.statuses))
        if query.campaign_id is not None:
            statement = statement.where(Inbox.campaign_id == query.campaign_id)
        if query.expiring_only:
            statement = statement.where(
                Inbox.status == InboxStatus.ACTIVE.value, Inbox.expires_at.is_not(None)
            )
        if query.search:
            pattern = f"%{query.search.strip().lower()}%"
            statement = statement.where(
                func.lower(Inbox.address).like(pattern) | func.lower(Inbox.label).like(pattern)
            )

        total = (
            await self._session.execute(select(func.count()).select_from(statement.subquery()))
        ).scalar_one()
        rows = (
            (
                await self._session.execute(
                    statement.order_by(Inbox.id.desc()).limit(query.limit).offset(query.offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def list_active_inboxes(self, limit: int = 100) -> list[Inbox]:
        """Return active inboxes that have not expired, most recently created first."""
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(Inbox)
            .where(
                Inbox.status == InboxStatus.ACTIVE.value,
                (Inbox.expires_at.is_(None)) | (Inbox.expires_at > now),
            )
            .order_by(Inbox.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def mark_expired(self, now: datetime | None = None) -> int:
        """Move every overdue active inbox to ``expired``. Returns how many changed."""
        moment = now or datetime.now(UTC)
        result = await self._session.execute(
            update(Inbox)
            .where(
                Inbox.status == InboxStatus.ACTIVE.value,
                Inbox.expires_at.is_not(None),
                Inbox.expires_at < moment,
            )
            .values(status=InboxStatus.EXPIRED.value, updated_at=moment)
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()
        return int(result.rowcount or 0)

    async def delete_inbox(self, inbox: Inbox) -> None:
        """Delete an inbox and (by cascade) its messages."""
        await self._session.delete(inbox)
        await self._session.flush()

    # ------------------------------------------------------------------ messages

    async def add_message(self, message: Message) -> Message:
        """Persist a new message."""
        self._session.add(message)
        await self._session.flush()
        return message

    async def get_message(self, message_id: int) -> Message | None:
        """Return one message by id."""
        return await self._session.get(Message, message_id)

    async def known_backend_ids(self, inbox_id: int) -> set[str]:
        """Return the backend ids already stored for an inbox."""
        result = await self._session.execute(
            select(Message.backend_message_id).where(
                Message.inbox_id == inbox_id, Message.backend_message_id.is_not(None)
            )
        )
        return {value for value in result.scalars().all() if value}

    async def list_messages(
        self,
        inbox_id: int,
        *,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Message], int]:
        """Return one page of an inbox's messages, newest first."""
        statement = select(Message).where(Message.inbox_id == inbox_id)
        if search:
            pattern = f"%{search.strip().lower()}%"
            statement = statement.where(
                func.lower(Message.subject).like(pattern)
                | func.lower(Message.sender).like(pattern)
                | func.lower(Message.body_text).like(pattern)
            )
        total = (
            await self._session.execute(select(func.count()).select_from(statement.subquery()))
        ).scalar_one()
        rows = (
            (
                await self._session.execute(
                    statement.order_by(Message.received_at.desc(), Message.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def messages_for_export(self, inbox_id: int, limit: int) -> list[Message]:
        """Return messages for an export, newest first."""
        result = await self._session.execute(
            select(Message)
            .where(Message.inbox_id == inbox_id)
            .order_by(Message.received_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

"""Data access for the Proxy Manager.

A thin repository: it builds and executes queries, and returns ORM objects or counts. Business
rules (what to do with a failed check, whether credentials may be stored) live in the service.

Keeping query construction here means the filter/sort/paginate logic exists once and every
caller - list, export, batch check, dashboard stats - behaves identically. That is the value of
the Repository pattern; when it stops adding value (trivial ``session.get`` calls) we call the
session directly rather than adding ceremony.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.proxy_models import Proxy, ProxyCheck, ProxySource

#: Sortable columns and their direction prefixes. Anything else is rejected by the router.
SORT_FIELDS: dict[str, Any] = {
    "id": Proxy.id,
    "host": Proxy.host,
    "port": Proxy.port,
    "protocol": Proxy.protocol,
    "status": Proxy.status,
    "latency_ms": Proxy.latency_ms,
    "last_checked_at": Proxy.last_checked_at,
    "created_at": Proxy.created_at,
    "success_count": Proxy.success_count,
    "failure_count": Proxy.failure_count,
}


@dataclass(slots=True)
class ProxyQuery:
    """Filter, sort and paginate options for a proxy list.

    A dataclass instead of a dozen keyword arguments keeps call sites readable and lets the same
    object be passed to ``list``, ``count`` and ``export``.
    """

    statuses: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    source_id: int | None = None
    search: str | None = None
    enabled_only: bool = False
    min_latency_ms: int | None = None
    max_latency_ms: int | None = None
    sort: str = "-created_at"
    limit: int = 50
    offset: int = 0

    def sort_column(self) -> tuple[Any, bool]:
        """Resolve ``sort`` to a column and a descending flag.

        ``-`` prefixes descending order: ``-latency_ms`` means fastest last. Unknown fields fall
        back to newest-first rather than raising, because a bad sort key should degrade the view,
        not break it.
        """
        field_name = self.sort[1:] if self.sort.startswith("-") else self.sort
        descending = self.sort.startswith("-")
        column = SORT_FIELDS.get(field_name, Proxy.created_at)
        return column, descending


class ProxyRepository:
    """Queries for proxies, sources and check history."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ sources

    async def add_source(self, source: ProxySource) -> ProxySource:
        """Persist a new import batch."""
        self._session.add(source)
        await self._session.flush()
        return source

    async def get_source(self, source_id: int) -> ProxySource | None:
        """Return one import batch by id."""
        return await self._session.get(ProxySource, source_id)

    # ------------------------------------------------------------------ proxies

    async def add_proxy(self, proxy: Proxy) -> Proxy:
        """Persist one proxy and flush so its id is available."""
        self._session.add(proxy)
        await self._session.flush()
        return proxy

    async def get_proxy(self, proxy_id: int) -> Proxy | None:
        """Return one proxy by id."""
        return await self._session.get(Proxy, proxy_id)

    async def get_proxies(self, proxy_ids: list[int]) -> list[Proxy]:
        """Return the proxies matching ``proxy_ids``, in id order."""
        if not proxy_ids:
            return []
        result = await self._session.execute(
            select(Proxy).where(Proxy.id.in_(proxy_ids)).order_by(Proxy.id)
        )
        return list(result.scalars().all())

    async def existing_identity_keys(self) -> set[str]:
        """Return the identity keys of every stored proxy.

        Used to de-duplicate an import against what is already stored. The key format must match
        :attr:`app.modules.proxy_manager.parser.ParsedProxy.identity_key`; a mismatch would let
        duplicates through, so it is computed in one place here.
        """
        result = await self._session.execute(
            select(Proxy.protocol, Proxy.host, Proxy.port, Proxy.username)
        )
        return {
            f"{protocol}|{host}|{port}|{username or ''}"
            for protocol, host, port, username in result.all()
        }

    def _apply_filters(self, statement: Select[Any], query: ProxyQuery) -> Select[Any]:
        """Apply every filter in ``query`` to ``statement``."""
        if query.statuses:
            statement = statement.where(Proxy.status.in_(query.statuses))
        if query.protocols:
            statement = statement.where(Proxy.protocol.in_(query.protocols))
        if query.source_id is not None:
            statement = statement.where(Proxy.source_id == query.source_id)
        if query.enabled_only:
            statement = statement.where(Proxy.is_enabled.is_(True))
        if query.min_latency_ms is not None:
            statement = statement.where(Proxy.latency_ms >= query.min_latency_ms)
        if query.max_latency_ms is not None:
            statement = statement.where(Proxy.latency_ms <= query.max_latency_ms)
        if query.search:
            # Case-insensitive substring match on the host only. Searching credentials would be
            # a way to confirm a password character by character.
            pattern = f"%{query.search.strip().lower()}%"
            statement = statement.where(
                or_(func.lower(Proxy.host).like(pattern), func.lower(Proxy.username).like(pattern))
            )
        return statement

    async def list_proxies(self, query: ProxyQuery) -> tuple[list[Proxy], int]:
        """Return one page of proxies and the total matching count."""
        column, descending = query.sort_column()
        ordering = column.desc() if descending else column.asc()

        statement = self._apply_filters(select(Proxy), query)
        total = (
            await self._session.execute(select(func.count()).select_from(statement.subquery()))
        ).scalar_one()

        # A stable secondary key keeps pagination deterministic when the primary key ties.
        statement = (
            statement.order_by(ordering, Proxy.id.asc()).limit(query.limit).offset(query.offset)
        )
        rows = (await self._session.execute(statement)).scalars().all()
        return list(rows), int(total)

    async def ids_matching(self, query: ProxyQuery) -> list[int]:
        """Return ids matching ``query``, capped by its ``limit``."""
        statement = (
            self._apply_filters(select(Proxy.id), query).order_by(Proxy.id.asc()).limit(query.limit)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def delete_proxy(self, proxy: Proxy) -> None:
        """Delete a proxy and (by cascade) its check history."""
        await self._session.delete(proxy)
        await self._session.flush()

    # ------------------------------------------------------------------ checks

    async def add_check(self, check: ProxyCheck) -> ProxyCheck:
        """Persist one health-check result."""
        self._session.add(check)
        await self._session.flush()
        return check

    async def recent_checks(self, proxy_id: int, limit: int = 20) -> list[ProxyCheck]:
        """Return a proxy's most recent checks, newest first."""
        result = await self._session.execute(
            select(ProxyCheck)
            .where(ProxyCheck.proxy_id == proxy_id)
            .order_by(ProxyCheck.checked_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # NOTE: history retention is intentionally absent here.
    # Pruning old checks belongs to the Phase 5 task manager (a scheduled retention job), and a
    # half-written pruner that silently deletes nothing would be worse than none at all - it would
    # look like the table is bounded when it is not. See docs/ROADMAP.md Phase 5.

    # ------------------------------------------------------------------ stats

    async def status_counts(self) -> dict[str, int]:
        """Count proxies per status."""
        result = await self._session.execute(
            select(Proxy.status, func.count()).group_by(Proxy.status)
        )
        return {status: int(count) for status, count in result.all()}

    async def total_count(self) -> int:
        """Count every stored proxy."""
        return int(
            (await self._session.execute(select(func.count()).select_from(Proxy))).scalar_one()
        )

    async def healthy_with_latency_count(self) -> int:
        """Count healthy proxies that have a measured latency."""
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(Proxy)
                    .where(Proxy.status == "healthy", Proxy.latency_ms.is_not(None))
                )
            ).scalar_one()
        )

    async def average_latency_ms(self, *, healthy_only: bool = True) -> float | None:
        """Average measured latency, rounded to one decimal.

        Averaging only healthy proxies is deliberate: including a 30-second timeout would make
        the number meaningless as a sense of "how fast is my pool".
        """
        statement = select(func.avg(Proxy.latency_ms)).where(Proxy.latency_ms.is_not(None))
        if healthy_only:
            statement = statement.where(Proxy.status == "healthy")
        value = (await self._session.execute(statement)).scalar_one_or_none()
        return None if value is None else round(float(value), 1)

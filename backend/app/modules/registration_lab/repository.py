"""Data access for the Registration Lab."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registration_models import (
    Campaign,
    RegistrationResult,
    RegistrationTarget,
    RegistrationTest,
)


@dataclass(slots=True)
class CampaignQuery:
    """Filter and paginate options for a campaign list."""

    statuses: list[str] | None = None
    limit: int = 50
    offset: int = 0


class RegistrationRepository:
    """Queries for targets, campaigns, tests and results."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ targets

    async def add_target(self, target: RegistrationTarget) -> RegistrationTarget:
        self._session.add(target)
        await self._session.flush()
        return target

    async def get_target(self, target_id: int) -> RegistrationTarget | None:
        return await self._session.get(RegistrationTarget, target_id)

    async def get_target_by_url(self, base_url: str) -> RegistrationTarget | None:
        result = await self._session.execute(
            select(RegistrationTarget).where(RegistrationTarget.base_url == base_url)
        )
        return result.scalar_one_or_none()

    async def list_targets(self) -> list[RegistrationTarget]:
        result = await self._session.execute(
            select(RegistrationTarget).order_by(RegistrationTarget.id)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ campaigns

    async def add_campaign(self, campaign: Campaign) -> Campaign:
        self._session.add(campaign)
        await self._session.flush()
        return campaign

    async def get_campaign(self, campaign_id: int) -> Campaign | None:
        return await self._session.get(Campaign, campaign_id)

    async def get_campaign_by_public_id(self, public_id: str) -> Campaign | None:
        result = await self._session.execute(
            select(Campaign).where(Campaign.public_id == public_id)
        )
        return result.scalar_one_or_none()

    async def list_campaigns(self, query: CampaignQuery) -> tuple[list[Campaign], int]:
        statement = select(Campaign)
        if query.statuses:
            statement = statement.where(Campaign.status.in_(query.statuses))
        total = (
            await self._session.execute(select(func.count()).select_from(statement.subquery()))
        ).scalar_one()
        rows = (
            (
                await self._session.execute(
                    statement.order_by(Campaign.id.desc()).limit(query.limit).offset(query.offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    # ------------------------------------------------------------------ tests

    async def add_test(self, test: RegistrationTest) -> RegistrationTest:
        self._session.add(test)
        await self._session.flush()
        return test

    async def tests_for_campaign(self, campaign_id: int) -> list[RegistrationTest]:
        result = await self._session.execute(
            select(RegistrationTest)
            .where(RegistrationTest.campaign_id == campaign_id)
            .order_by(RegistrationTest.id)
        )
        return list(result.scalars().all())

    async def pending_tests(self, campaign_id: int) -> list[RegistrationTest]:
        """Return tests that have not reached a terminal state yet."""
        result = await self._session.execute(
            select(RegistrationTest)
            .where(
                RegistrationTest.campaign_id == campaign_id,
                RegistrationTest.status.in_(["pending", "submitted", "awaiting_email"]),
            )
            .order_by(RegistrationTest.id)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ results

    async def add_result(self, result: RegistrationResult) -> RegistrationResult:
        self._session.add(result)
        await self._session.flush()
        return result

    async def results_for_campaign(
        self, campaign_id: int, *, limit: int = 200, offset: int = 0
    ) -> tuple[list[tuple[RegistrationTest, RegistrationResult | None]], int]:
        total = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(RegistrationTest)
                    .where(RegistrationTest.campaign_id == campaign_id)
                )
            ).scalar_one()
        )
        result = await self._session.execute(
            select(RegistrationTest, RegistrationResult)
            .outerjoin(RegistrationResult, RegistrationResult.test_id == RegistrationTest.id)
            .where(RegistrationTest.campaign_id == campaign_id)
            .order_by(RegistrationTest.id)
            .limit(limit)
            .offset(offset)
        )
        return [(test, outcome) for test, outcome in result.all()], total

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.billing import Subscription
from app.models.enums import Plan, SubscriptionStatus
from app.models.tenancy import Organization
from app.services.audit import write_audit
from app.services.usage import calendar_period, subscription_period


async def create_default_subscription(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> Subscription:
    period_start, period_end = calendar_period(now)
    subscription = Subscription(
        organization_id=organization_id,
        plan=Plan.FREE,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=period_start,
        current_period_end=period_end,
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def get_subscription(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> Subscription:
    subscription = await session.scalar(
        select(Subscription).where(Subscription.organization_id == organization_id)
    )
    if subscription is None:
        raise NotFoundError("subscription_not_found", "Subscription not found")
    return subscription


async def get_or_create_subscription(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> Subscription:
    organization = await session.scalar(
        select(Organization)
        .where(Organization.id == organization_id)
        .with_for_update()
    )
    if organization is None:
        raise NotFoundError("organization_not_found", "Organization not found")
    subscription = await session.scalar(
        select(Subscription).where(Subscription.organization_id == organization_id)
    )
    if subscription is not None:
        return subscription
    return await create_default_subscription(session, organization_id, now=now)


async def change_plan(
    session: AsyncSession,
    organization_id: uuid.UUID,
    plan: Plan,
    *,
    actor_user_id: uuid.UUID,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> Subscription:
    subscription = await get_or_create_subscription(session, organization_id, now=now)
    previous_plan = subscription.plan
    period_start, period_end = subscription_period(subscription, now)
    subscription.plan = plan
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.current_period_start = period_start
    subscription.current_period_end = period_end
    await session.flush()
    if previous_plan != plan:
        await write_audit(
            session,
            organization_id,
            actor_user_id,
            "subscription.changed",
            "subscription",
            subscription.id,
            {"from_plan": previous_plan.value, "to_plan": plan.value},
            ip_address,
        )
    await session.refresh(subscription)
    return subscription

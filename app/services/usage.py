import calendar
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import PLAN_QUOTAS
from app.core.errors import AppError, NotFoundError
from app.models.billing import Subscription, UsageEvent
from app.models.enums import Plan
from app.models.tenancy import Organization

API_REQUESTS_METRIC = "api_requests"


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    plan: Plan
    used: int
    limit: int
    remaining: int
    period_start: datetime
    period_end: datetime


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def add_month(value: datetime) -> datetime:
    value = _as_utc(value)
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def calendar_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = _as_utc(now or datetime.now(UTC))
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, add_month(start)


def subscription_period(
    subscription: Subscription | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    current = _as_utc(now or datetime.now(UTC))
    if subscription is None:
        return calendar_period(current)

    start = _as_utc(subscription.current_period_start)
    end = _as_utc(subscription.current_period_end)
    if end <= start:
        return calendar_period(current)
    if current < start:
        return start, end
    while current >= end:
        start = end
        end = add_month(end)
    return start, end


async def _usage_total(
    session: AsyncSession,
    organization_id: uuid.UUID,
    metric: str,
    period_start: datetime,
    period_end: datetime,
) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
            UsageEvent.organization_id == organization_id,
            UsageEvent.metric == metric,
            UsageEvent.created_at >= period_start,
            UsageEvent.created_at < period_end,
        )
    )
    return int(total or 0)


async def get_usage_summary(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    metric: str = API_REQUESTS_METRIC,
    now: datetime | None = None,
) -> UsageSnapshot:
    organization_exists = await session.scalar(
        select(Organization.id).where(Organization.id == organization_id)
    )
    if organization_exists is None:
        raise NotFoundError("organization_not_found", "Organization not found")

    subscription = await session.scalar(
        select(Subscription).where(Subscription.organization_id == organization_id)
    )
    plan = subscription.plan if subscription is not None else Plan.FREE
    period_start, period_end = subscription_period(subscription, now)
    used = await _usage_total(session, organization_id, metric, period_start, period_end)
    limit = PLAN_QUOTAS[plan.value]
    return UsageSnapshot(
        plan=plan,
        used=used,
        limit=limit,
        remaining=max(0, limit - used),
        period_start=period_start,
        period_end=period_end,
    )


async def record_usage(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    metric: str = API_REQUESTS_METRIC,
    quantity: int = 1,
    now: datetime | None = None,
) -> UsageEvent:
    if quantity < 1:
        raise ValueError("quantity must be positive")

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
    plan = subscription.plan if subscription is not None else Plan.FREE
    period_start, period_end = subscription_period(subscription, now)
    used = await _usage_total(session, organization_id, metric, period_start, period_end)
    limit = PLAN_QUOTAS[plan.value]
    if used + quantity > limit:
        current = _as_utc(now or datetime.now(UTC))
        retry_after = max(1, int((period_end - current).total_seconds()))
        raise AppError(
            429,
            "quota_exceeded",
            f"Monthly {metric} quota exceeded",
            {"Retry-After": str(retry_after)},
        )

    event = UsageEvent(
        organization_id=organization_id,
        metric=metric,
        quantity=quantity,
        created_at=_as_utc(now) if now is not None else datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return event


check_and_record_usage = record_usage


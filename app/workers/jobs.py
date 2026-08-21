import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.metrics import WEBHOOK_EVENTS
from app.database import SessionLocal
from app.models.billing import UsageEvent
from app.services.webhooks import mark_webhook_failed, process_webhook_event

JobHandler = Callable[[dict[str, Any]], Awaitable[None]]
logger = structlog.get_logger()


def _required(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is None or value == "":
        raise ValueError(f"job payload is missing {key}")
    return value


async def send_invitation_email(payload: dict[str, Any]) -> None:
    recipient = str(_required(payload, "email"))
    organization_id = str(_required(payload, "organization_id"))
    invitation_id = str(_required(payload, "invitation_id"))
    logger.info(
        "invitation_email_sent",
        recipient=recipient,
        organization_id=organization_id,
        invitation_id=invitation_id,
    )


async def process_billing_webhook_job(
    payload: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> None:
    event_id = str(_required(payload, "webhook_event_id"))
    try:
        async with session_factory() as session:
            event, processed = await process_webhook_event(session, event_id)
            event_type = event.event_type
            await session.commit()
    except Exception:
        async with session_factory() as session:
            await mark_webhook_failed(session, event_id)
            await session.commit()
        WEBHOOK_EVENTS.labels(event_type="processing", outcome="failed").inc()
        raise
    outcome = "processed" if processed else "already_processed"
    WEBHOOK_EVENTS.labels(event_type=event_type, outcome=outcome).inc()


def _parse_time(value: Any, *, default: datetime) -> datetime:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("period timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def write_usage_rollup(
    payload: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> None:
    organization_id = uuid.UUID(str(_required(payload, "organization_id")))
    metric = str(payload.get("metric", "api_requests"))
    period_end = _parse_time(payload.get("period_end"), default=datetime.now(UTC))
    period_start = _parse_time(
        payload.get("period_start"),
        default=period_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
    )
    if period_end <= period_start:
        raise ValueError("period_end must be after period_start")
    async with session_factory() as session:
        quantity = await session.scalar(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.organization_id == organization_id,
                UsageEvent.metric == metric,
                UsageEvent.created_at >= period_start,
                UsageEvent.created_at < period_end,
            )
        )
    logger.info(
        "usage_rollup_calculated",
        organization_id=str(organization_id),
        metric=metric,
        quantity=int(quantity or 0),
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
    )


def build_handlers(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> dict[str, JobHandler]:
    async def billing_handler(payload: dict[str, Any]) -> None:
        await process_billing_webhook_job(payload, session_factory=session_factory)

    async def rollup_handler(payload: dict[str, Any]) -> None:
        await write_usage_rollup(payload, session_factory=session_factory)

    return {
        "send_invitation_email": send_invitation_email,
        "process_billing_webhook": billing_handler,
        "write_usage_rollup": rollup_handler,
    }

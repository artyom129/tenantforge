from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import AppError
from app.core.metrics import WEBHOOK_EVENTS
from app.database import get_session
from app.infrastructure.queue import RedisJobQueue
from app.infrastructure.redis import get_redis
from app.models.enums import WebhookStatus
from app.schemas.billing import BillingWebhook, WebhookReceipt
from app.services.webhooks import (
    store_webhook_event,
    validate_webhook,
    verify_webhook_signature,
)

router = APIRouter(prefix="/webhooks", tags=["Billing"])
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]
RedisConnection = Annotated[Redis, Depends(get_redis)]


@router.post(
    "/billing",
    response_model=WebhookReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
async def billing_webhook(
    request: Request,
    session: Session,
    redis: RedisConnection,
) -> WebhookReceipt:
    body = await request.body()
    try:
        verify_webhook_signature(
            body,
            request.headers.get("Stripe-Signature"),
            get_settings().billing_webhook_secret.get_secret_value(),
        )
        event = BillingWebhook.model_validate_json(body)
        validate_webhook(event)
    except ValidationError as exc:
        WEBHOOK_EVENTS.labels(event_type="invalid", outcome="rejected").inc()
        raise AppError(400, "invalid_webhook_event", "Invalid webhook payload") from exc
    except AppError:
        WEBHOOK_EVENTS.labels(event_type="invalid", outcome="rejected").inc()
        raise

    stored_event, created = await store_webhook_event(session, event)
    await session.commit()
    should_enqueue = created or stored_event.status != WebhookStatus.PROCESSED
    if should_enqueue:
        queue = RedisJobQueue(redis)
        await queue.enqueue(
            "process_billing_webhook",
            {"webhook_event_id": str(stored_event.id)},
        )
    outcome = "accepted" if created else "duplicate"
    WEBHOOK_EVENTS.labels(event_type=event.type, outcome=outcome).inc()
    return WebhookReceipt(
        event_id=stored_event.id,
        status=stored_event.status,
        duplicate=not created,
    )

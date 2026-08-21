import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.models.billing import Subscription, WebhookEvent
from app.models.enums import Plan, SubscriptionStatus, WebhookStatus
from app.models.tenancy import Organization
from app.schemas.billing import BillingWebhook
from app.services.audit import write_audit
from app.services.billing import create_default_subscription
from app.services.usage import calendar_period

SUPPORTED_EVENTS = {
    "subscription.created",
    "subscription.updated",
    "subscription.cancelled",
    "invoice.payment_failed",
}
SIGNATURE_TOLERANCE_SECONDS = 300
PROVIDER = "stripe-like"


def build_webhook_signature(payload: bytes, secret: str, timestamp: int) -> str:
    signed_payload = str(timestamp).encode() + b"." + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_webhook_signature(
    payload: bytes,
    signature_header: str | None,
    secret: str,
    *,
    now: float | None = None,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> None:
    if not signature_header:
        raise AppError(400, "invalid_webhook_signature", "Missing webhook signature")

    values: dict[str, list[str]] = {}
    for part in signature_header.split(","):
        key, separator, value = part.strip().partition("=")
        if separator and value:
            values.setdefault(key, []).append(value)
    try:
        timestamp = int(values["t"][0])
    except (KeyError, ValueError):
        raise AppError(400, "invalid_webhook_signature", "Invalid webhook signature") from None

    current_time = time.time() if now is None else now
    if tolerance < 0 or abs(current_time - timestamp) > tolerance:
        raise AppError(400, "invalid_webhook_signature", "Webhook signature has expired")

    signed_payload = str(timestamp).encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    signatures = values.get("v1", [])
    if not signatures or not any(hmac.compare_digest(expected, value) for value in signatures):
        raise AppError(400, "invalid_webhook_signature", "Invalid webhook signature")


def validate_webhook(event: BillingWebhook) -> dict[str, Any]:
    if event.type not in SUPPORTED_EVENTS:
        raise AppError(400, "unsupported_webhook_event", "Unsupported webhook event")
    event_object = event.data.get("object")
    if not isinstance(event_object, dict):
        raise AppError(400, "invalid_webhook_event", "Webhook data.object must be an object")
    try:
        uuid.UUID(str(event_object["organization_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AppError(
            400,
            "invalid_webhook_event",
            "Webhook organization_id is invalid",
        ) from None
    if event.type in {"subscription.created", "subscription.updated"}:
        try:
            Plan(str(event_object["plan"]).upper())
        except (KeyError, ValueError):
            raise AppError(400, "invalid_webhook_event", "Webhook plan is invalid") from None
        try:
            if "status" in event_object:
                SubscriptionStatus(str(event_object["status"]).upper())
            has_start = "current_period_start" in event_object
            has_end = "current_period_end" in event_object
            if has_start != has_end:
                raise ValueError("billing period boundaries must be provided together")
            if has_start:
                period_start = _parse_datetime(
                    event_object["current_period_start"],
                    datetime.now(UTC),
                )
                period_end = _parse_datetime(
                    event_object["current_period_end"],
                    datetime.now(UTC),
                )
                if period_end <= period_start:
                    raise ValueError("billing period end must be after its start")
        except (OSError, OverflowError, TypeError, ValueError):
            raise AppError(
                400,
                "invalid_webhook_event",
                "Webhook subscription status or period is invalid",
            ) from None
        for field in ("customer_id", "subscription_id"):
            value = event_object.get(field)
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 120):
                raise AppError(400, "invalid_webhook_event", f"Webhook {field} is invalid")
    return event_object


async def store_webhook_event(
    session: AsyncSession,
    event: BillingWebhook,
) -> tuple[WebhookEvent, bool]:
    existing = await session.scalar(
        select(WebhookEvent).where(
            WebhookEvent.provider == PROVIDER,
            WebhookEvent.external_event_id == event.id,
        )
    )
    if existing is not None:
        return existing, False

    webhook = WebhookEvent(
        provider=PROVIDER,
        external_event_id=event.id,
        event_type=event.type,
        payload=event.model_dump(mode="json"),
        status=WebhookStatus.PENDING,
    )
    try:
        async with session.begin_nested():
            session.add(webhook)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(WebhookEvent).where(
                WebhookEvent.provider == PROVIDER,
                WebhookEvent.external_event_id == event.id,
            )
        )
        if existing is None:
            raise
        return existing, False
    return webhook, True


def _parse_datetime(value: Any, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str):
        raise ValueError("invalid billing period timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _locked_subscription(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> Subscription:
    organization = await session.scalar(
        select(Organization)
        .where(Organization.id == organization_id)
        .with_for_update()
    )
    if organization is None:
        raise ValueError("webhook organization does not exist")
    subscription = await session.scalar(
        select(Subscription)
        .where(Subscription.organization_id == organization_id)
        .with_for_update()
    )
    if subscription is None:
        subscription = await create_default_subscription(session, organization_id)
    return subscription


async def process_webhook_event(
    session: AsyncSession,
    webhook_event_id: uuid.UUID | str,
    *,
    now: datetime | None = None,
) -> tuple[WebhookEvent, bool]:
    event_id = uuid.UUID(str(webhook_event_id))
    event = await session.scalar(
        select(WebhookEvent).where(WebhookEvent.id == event_id).with_for_update()
    )
    if event is None:
        raise NotFoundError("webhook_event_not_found", "Webhook event not found")
    if event.status == WebhookStatus.PROCESSED:
        return event, False

    parsed_event = BillingWebhook.model_validate(event.payload)
    event_object = validate_webhook(parsed_event)
    organization_id = uuid.UUID(str(event_object["organization_id"]))
    subscription = await _locked_subscription(session, organization_id)
    previous_plan = subscription.plan
    previous_status = subscription.status
    period_start, period_end = calendar_period(now)

    if event.event_type in {"subscription.created", "subscription.updated"}:
        subscription.plan = Plan(str(event_object["plan"]).upper())
        raw_status = str(event_object.get("status", SubscriptionStatus.ACTIVE.value)).upper()
        subscription.status = SubscriptionStatus(raw_status)
        if "customer_id" in event_object:
            subscription.external_customer_id = event_object["customer_id"]
        if "subscription_id" in event_object:
            subscription.external_subscription_id = event_object["subscription_id"]
        subscription.current_period_start = _parse_datetime(
            event_object.get("current_period_start"), period_start
        )
        subscription.current_period_end = _parse_datetime(
            event_object.get("current_period_end"), period_end
        )
        if subscription.current_period_end <= subscription.current_period_start:
            raise ValueError("billing period end must be after its start")
    elif event.event_type == "subscription.cancelled":
        subscription.status = SubscriptionStatus.CANCELED
    elif event.event_type == "invoice.payment_failed":
        subscription.status = SubscriptionStatus.PAST_DUE

    await write_audit(
        session,
        organization_id,
        None,
        "subscription.changed",
        "subscription",
        subscription.id,
        {
            "event_id": event.external_event_id,
            "event_type": event.event_type,
            "from_plan": previous_plan.value,
            "to_plan": subscription.plan.value,
            "from_status": previous_status.value,
            "to_status": subscription.status.value,
        },
    )
    event.status = WebhookStatus.PROCESSED
    event.processed_at = now or datetime.now(UTC)
    await session.flush()
    return event, True


async def mark_webhook_failed(
    session: AsyncSession,
    webhook_event_id: uuid.UUID | str,
) -> None:
    event = await session.scalar(
        select(WebhookEvent)
        .where(WebhookEvent.id == uuid.UUID(str(webhook_event_id)))
        .with_for_update()
    )
    if event is not None and event.status != WebhookStatus.PROCESSED:
        event.status = WebhookStatus.FAILED
        await session.flush()

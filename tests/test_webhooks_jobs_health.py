import json
import time
import uuid

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.infrastructure.queue import RedisJobQueue
from app.models.audit import AuditLog
from app.models.billing import Subscription, WebhookEvent
from app.models.enums import Plan, SubscriptionStatus, WebhookStatus
from app.schemas.billing import BillingWebhook
from app.services.webhooks import (
    build_webhook_signature,
    process_webhook_event,
    store_webhook_event,
    verify_webhook_signature,
)
from app.workers.worker import Worker


def webhook_request(
    organization_id,
    *,
    event_id: str | None = None,
    event_type: str = "subscription.updated",
    plan: str = "PRO",
    timestamp: int | None = None,
) -> tuple[bytes, str]:
    payload = {
        "id": event_id or f"evt_{uuid.uuid4().hex}",
        "type": event_type,
        "data": {
            "object": {
                "organization_id": str(organization_id),
                "plan": plan,
                "status": "ACTIVE",
                "customer_id": "cus_test",
                "subscription_id": f"sub_{uuid.uuid4().hex}",
                "current_period_start": "2026-08-01T00:00:00Z",
                "current_period_end": "2026-09-01T00:00:00Z",
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signed_at = int(time.time()) if timestamp is None else timestamp
    signature = build_webhook_signature(
        body,
        get_settings().billing_webhook_secret.get_secret_value(),
        signed_at,
    )
    return body, signature


@pytest.mark.asyncio
async def test_valid_webhook_is_persisted_and_enqueued(
    client,
    db_session,
    redis_client,
    organization_factory,
):
    organization, _ = await organization_factory()
    body, signature = webhook_request(organization.id)

    response = await client.post(
        "/webhooks/billing",
        content=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": signature},
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "PENDING"
    assert response.json()["duplicate"] is False
    stored = await db_session.scalar(select(WebhookEvent))
    assert stored.external_event_id == json.loads(body)["id"]
    assert await redis_client.llen("tenantforge:jobs") == 1


@pytest.mark.asyncio
async def test_duplicate_webhook_has_one_database_record(
    client,
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    body, signature = webhook_request(organization.id, event_id="evt_duplicate")
    headers = {"Content-Type": "application/json", "Stripe-Signature": signature}

    first = await client.post("/webhooks/billing", content=body, headers=headers)
    duplicate = await client.post("/webhooks/billing", content=body, headers=headers)

    assert first.status_code == duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    count = await db_session.scalar(select(func.count()).select_from(WebhookEvent))
    assert count == 1


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature(
    client,
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    body, _ = webhook_request(organization.id)

    response = await client.post(
        "/webhooks/billing",
        content=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": "t=1,v1=bad"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_webhook_signature"
    count = await db_session.scalar(select(func.count()).select_from(WebhookEvent))
    assert count == 0


def test_webhook_signature_rejects_replay_after_tolerance():
    payload = b"{}"
    secret = "test-secret"
    signature = build_webhook_signature(payload, secret, 1_000)

    with pytest.raises(Exception) as error:
        verify_webhook_signature(payload, signature, secret, now=1_301)

    assert getattr(error.value, "code", None) == "invalid_webhook_signature"


@pytest.mark.asyncio
async def test_webhook_rejects_unsupported_event_type(
    client,
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    body, signature = webhook_request(organization.id, event_type="customer.deleted")

    response = await client.post(
        "/webhooks/billing",
        content=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": signature},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_webhook_event"
    count = await db_session.scalar(select(func.count()).select_from(WebhookEvent))
    assert count == 0


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"status": "NOT_A_STATUS"},
        {
            "current_period_start": "not-a-date",
            "current_period_end": "2026-09-01T00:00:00Z",
        },
        {"current_period_start": "2999-01-01T00:00:00Z", "current_period_end": None},
        {
            "current_period_start": "2026-09-01T00:00:00Z",
            "current_period_end": "2026-08-01T00:00:00Z",
        },
    ],
)
@pytest.mark.asyncio
async def test_webhook_rejects_invalid_subscription_fields(
    invalid_fields,
    client,
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    original_body, _ = webhook_request(organization.id)
    payload = json.loads(original_body)
    payload["data"]["object"].update(invalid_fields)
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = build_webhook_signature(
        body,
        get_settings().billing_webhook_secret.get_secret_value(),
        int(time.time()),
    )

    response = await client.post(
        "/webhooks/billing",
        content=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": signature},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_webhook_event"
    count = await db_session.scalar(select(func.count()).select_from(WebhookEvent))
    assert count == 0


@pytest.mark.asyncio
async def test_worker_processing_updates_subscription_and_event(
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    body, _ = webhook_request(organization.id, event_id="evt_process")
    event, created = await store_webhook_event(
        db_session,
        BillingWebhook.model_validate_json(body),
    )
    await db_session.commit()

    processed_event, processed = await process_webhook_event(db_session, event.id)
    await db_session.commit()
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.organization_id == organization.id)
    )
    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.organization_id == organization.id)
    )

    assert created is True
    assert processed is True
    assert processed_event.status == WebhookStatus.PROCESSED
    assert processed_event.processed_at is not None
    assert subscription.plan == Plan.PRO
    assert subscription.external_customer_id == "cus_test"
    assert audit_count == 1


@pytest.mark.asyncio
async def test_processed_webhook_does_not_repeat_business_logic(
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    body, _ = webhook_request(organization.id, event_id="evt_once")
    event, _ = await store_webhook_event(
        db_session,
        BillingWebhook.model_validate_json(body),
    )
    await db_session.commit()
    await process_webhook_event(db_session, event.id)
    await db_session.commit()

    _, processed_again = await process_webhook_event(db_session, event.id)
    await db_session.commit()
    audit_count = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.organization_id == organization.id)
    )

    assert processed_again is False
    assert audit_count == 1


@pytest.mark.asyncio
async def test_payment_failed_webhook_marks_subscription_past_due(
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    body, _ = webhook_request(
        organization.id,
        event_id="evt_payment_failed",
        event_type="invoice.payment_failed",
    )
    event, _ = await store_webhook_event(
        db_session,
        BillingWebhook.model_validate_json(body),
    )
    await db_session.commit()

    await process_webhook_event(db_session, event.id)
    await db_session.commit()
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.organization_id == organization.id)
    )

    assert subscription.status == SubscriptionStatus.PAST_DUE


@pytest.mark.asyncio
async def test_worker_executes_successful_job(redis_client):
    queue = RedisJobQueue(redis_client, queue_name="test:success")
    received = []

    async def handler(payload):
        received.append(payload)

    await queue.enqueue("record", {"value": 42})
    worker = Worker(queue, {"record": handler})

    assert await worker.run_once(timeout=1) is True
    assert received == [{"value": 42}]
    assert await redis_client.llen("test:success") == 0
    assert await redis_client.llen("test:success:processing") == 0


@pytest.mark.asyncio
async def test_queue_recovers_job_left_in_flight(redis_client):
    queue = RedisJobQueue(redis_client, queue_name="test:recover")
    queued = await queue.enqueue("record", {"value": 42})

    in_flight = await queue.dequeue(timeout=1)
    assert in_flight.id == queued.id
    assert await redis_client.llen("test:recover") == 0
    assert await redis_client.llen("test:recover:processing") == 1

    assert await queue.recover_in_flight() == 1
    recovered = await queue.dequeue(timeout=1)
    assert recovered.id == queued.id
    await queue.acknowledge(recovered)
    assert await redis_client.llen("test:recover:processing") == 0


@pytest.mark.asyncio
async def test_queue_dead_letters_malformed_envelope_without_copying_it(redis_client):
    queue = RedisJobQueue(redis_client, queue_name="test:malformed")
    await redis_client.lpush("test:malformed", "not-json-and-not-a-secret-safe-envelope")

    with pytest.raises(json.JSONDecodeError):
        await queue.dequeue(timeout=1)

    assert await redis_client.llen("test:malformed:processing") == 0
    envelope = json.loads(await redis_client.lindex("tenantforge:jobs:dead", 0))
    assert envelope["error_type"] == "JSONDecodeError"
    assert "not-json" not in json.dumps(envelope)


@pytest.mark.asyncio
async def test_worker_retries_with_injected_sleep(redis_client):
    queue = RedisJobQueue(redis_client, queue_name="test:retry")
    sleeps = []

    async def fail(_payload):
        raise RuntimeError("transient")

    async def no_sleep(delay):
        sleeps.append(delay)

    await queue.enqueue("fails", {})
    worker = Worker(queue, {"fails": fail}, sleep=no_sleep)
    await worker.run_once(timeout=1)
    retried = await queue.dequeue(timeout=1)

    assert sleeps == [1]
    assert retried.attempt == 1


@pytest.mark.asyncio
async def test_worker_dead_letters_after_three_retries(redis_client):
    queue = RedisJobQueue(
        redis_client,
        queue_name="test:dlq",
        dead_letter_queue="test:dead",
    )
    sleeps = []

    async def fail(_payload):
        raise RuntimeError("permanent")

    async def no_sleep(delay):
        sleeps.append(delay)

    invitation_token = "one-time-invitation-secret"
    await queue.enqueue(
        "fails",
        {"safe": "payload", "token": invitation_token, "nested": {"password": "secret"}},
    )
    worker = Worker(queue, {"fails": fail}, sleep=no_sleep)
    for _ in range(4):
        await worker.run_once(timeout=1)

    assert sleeps == [1, 2, 4]
    assert await redis_client.llen("test:dlq") == 0
    envelope = json.loads(await redis_client.lindex("test:dead", 0))
    assert envelope["job"]["attempt"] == 3
    assert envelope["error_type"] == "RuntimeError"
    assert envelope["job"]["payload"] == {
        "safe": "payload",
        "token": "[redacted]",
        "nested": {"password": "[redacted]"},
    }
    assert invitation_token not in json.dumps(envelope)


@pytest.mark.asyncio
async def test_live_endpoint(client):
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_endpoint_checks_database_and_redis(client):
    response = await client.get("/health/ready")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


@pytest.mark.asyncio
async def test_ready_endpoint_returns_503_when_redis_is_down(
    client,
    redis_client,
    monkeypatch,
):
    async def unavailable():
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(redis_client, "ping", unavailable)
    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["redis"] == "unavailable"
    assert response.json()["postgres"] == "ok"


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_text(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text
    assert "background_jobs_total" in response.text
    assert "webhook_events_total" in response.text

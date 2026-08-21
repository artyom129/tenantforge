import asyncio
import json
import os
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import get_settings
from app.main import app
from app.models.audit import AuditLog
from app.models.billing import WebhookEvent
from app.models.identity import RefreshToken, User
from app.services.webhooks import build_webhook_signature, process_webhook_event
from tests.helpers import create_organization, register_and_login

pytestmark = pytest.mark.skipif(
    os.environ.get("TEST_USE_EXTERNAL_SERVICES") != "1",
    reason="requires PostgreSQL row locking and uniqueness waits",
)


async def _simultaneous_posts(path: str, **request_kwargs):
    start = asyncio.Event()

    async def post_once(request_client: AsyncClient):
        await start.wait()
        return await request_client.post(path, **request_kwargs)

    first_transport = ASGITransport(app=app, raise_app_exceptions=False)
    second_transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=first_transport, base_url="http://testserver") as first_client,
        AsyncClient(transport=second_transport, base_url="http://testserver") as second_client,
    ):
        first_task = asyncio.create_task(post_once(first_client))
        second_task = asyncio.create_task(post_once(second_client))
        start.set()
        return await asyncio.gather(first_task, second_task)


@pytest.mark.asyncio
async def test_concurrent_refresh_rotation_has_one_winner(
    client,
    session_factory,
) -> None:
    tokens = await register_and_login(client, email="refresh-race@example.test")

    first, second = await _simultaneous_posts(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert sorted((first.status_code, second.status_code)) == [200, 401]
    rejected = first if first.status_code == 401 else second
    assert rejected.json()["error"]["code"] == "refresh_token_reused"
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.email == "refresh-race@example.test")
        )
        stored_tokens = list(
            await session.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id))
        )
    assert len(stored_tokens) == 2
    assert sum(token.revoked_at is None for token in stored_tokens) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_webhook_runs_business_logic_once(
    client,
    session_factory,
) -> None:
    tokens = await register_and_login(client, email="webhook-race@example.test")
    organization = await create_organization(client, tokens["access_token"])
    payload = {
        "id": "evt_concurrent_duplicate",
        "type": "subscription.updated",
        "data": {
            "object": {
                "organization_id": organization["id"],
                "plan": "PRO",
                "status": "ACTIVE",
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = build_webhook_signature(
        body,
        get_settings().billing_webhook_secret.get_secret_value(),
        int(time.time()),
    )

    first, second = await _simultaneous_posts(
        "/webhooks/billing",
        content=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": signature},
    )

    assert first.status_code == second.status_code == 202
    assert sorted((first.json()["duplicate"], second.json()["duplicate"])) == [False, True]
    async with session_factory() as session:
        event = await session.scalar(
            select(WebhookEvent).where(
                WebhookEvent.external_event_id == "evt_concurrent_duplicate"
            )
        )

    async def process_once() -> bool:
        async with session_factory() as session:
            _, processed = await process_webhook_event(session, event.id)
            await session.commit()
            return processed

    processed = await asyncio.gather(process_once(), process_once())
    assert sorted(processed) == [False, True]
    async with session_factory() as session:
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.organization_id
                == uuid.UUID(event.payload["data"]["object"]["organization_id"]),
                AuditLog.action == "subscription.changed",
            )
        )
    assert audit_count == 1

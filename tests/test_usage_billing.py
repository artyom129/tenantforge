import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.core.errors import AppError
from app.main import app
from app.models.audit import AuditLog
from app.models.billing import Subscription, UsageEvent
from app.models.enums import Plan, Role
from app.models.resources import ApiKey
from app.security.jwt import create_access_token
from app.security.opaque_tokens import hash_token
from app.services.audit import write_audit
from app.services.usage import get_usage_summary, record_usage
from tests.helpers import auth_headers


async def create_test_api_key(session, organization, user) -> str:
    raw_key = f"tf_live_{uuid.uuid4().hex}"
    api_key = ApiKey(
        organization_id=organization.id,
        name="Data plane",
        key_hash=hash_token(raw_key),
        prefix=raw_key[:16],
        created_by=user.id,
    )
    session.add(api_key)
    await session.commit()
    return raw_key


@pytest.mark.asyncio
async def test_api_key_usage_event_tracks_usage(
    client,
    db_session,
    organization_factory,
    user_factory,
):
    user = await user_factory()
    organization, _ = await organization_factory(owner=user)
    raw_key = await create_test_api_key(db_session, organization, user)

    response = await client.post(
        f"/organizations/{organization.id}/usage/events",
        headers={"X-API-Key": raw_key},
        json={"quantity": 7},
    )

    assert response.status_code == 201, response.text
    assert response.json()["organization_id"] == str(organization.id)
    assert response.json()["metric"] == "api_requests"
    assert response.json()["quantity"] == 7
    total = await db_session.scalar(
        select(func.sum(UsageEvent.quantity)).where(
            UsageEvent.organization_id == organization.id
        )
    )
    assert total == 7


@pytest.mark.asyncio
async def test_api_key_cannot_write_usage_for_another_tenant(
    client,
    db_session,
    organization_factory,
    user_factory,
):
    user = await user_factory()
    organization, _ = await organization_factory(owner=user)
    other_organization, _ = await organization_factory()
    raw_key = await create_test_api_key(db_session, organization, user)

    response = await client.post(
        f"/organizations/{other_organization.id}/usage/events",
        headers={"X-API-Key": raw_key},
        json={"quantity": 1},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "organization_not_found"
    count = await db_session.scalar(select(func.count()).select_from(UsageEvent))
    assert count == 0


@pytest.mark.parametrize("quantity", [0, 1_001])
@pytest.mark.asyncio
async def test_usage_event_quantity_is_bounded(
    quantity,
    client,
    db_session,
    organization_factory,
    user_factory,
):
    user = await user_factory()
    organization, _ = await organization_factory(owner=user)
    raw_key = await create_test_api_key(db_session, organization, user)

    response = await client.post(
        f"/organizations/{organization.id}/usage/events",
        headers={"X-API-Key": raw_key},
        json={"quantity": quantity},
    )

    assert response.status_code == 422
    count = await db_session.scalar(select(func.count()).select_from(UsageEvent))
    assert count == 0


@pytest.mark.asyncio
async def test_record_usage_rejects_quota_without_writing(
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.organization_id == organization.id)
    )
    now = datetime(2026, 8, 15, tzinfo=UTC)
    subscription.current_period_start = datetime(2026, 8, 1, tzinfo=UTC)
    subscription.current_period_end = datetime(2026, 9, 1, tzinfo=UTC)
    db_session.add(
        UsageEvent(
            organization_id=organization.id,
            metric="api_requests",
            quantity=1_000,
            created_at=now,
        )
    )
    await db_session.commit()

    with pytest.raises(AppError) as error:
        await record_usage(db_session, organization.id, now=now)

    assert error.value.status_code == 429
    assert error.value.code == "quota_exceeded"
    assert int(error.value.headers["Retry-After"]) > 0
    count = await db_session.scalar(select(func.count()).select_from(UsageEvent))
    assert count == 1


@pytest.mark.asyncio
async def test_usage_period_reset_ignores_previous_period(
    db_session,
    organization_factory,
):
    organization, _ = await organization_factory()
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.organization_id == organization.id)
    )
    subscription.current_period_start = datetime(2026, 1, 1, tzinfo=UTC)
    subscription.current_period_end = datetime(2026, 2, 1, tzinfo=UTC)
    db_session.add(
        UsageEvent(
            organization_id=organization.id,
            metric="api_requests",
            quantity=900,
            created_at=datetime(2026, 1, 20, tzinfo=UTC),
        )
    )
    await db_session.commit()

    now = datetime(2026, 2, 15, tzinfo=UTC)
    await record_usage(db_session, organization.id, quantity=5, now=now)
    summary = await get_usage_summary(db_session, organization.id, now=now)

    assert summary.used == 5
    assert summary.remaining == 995
    assert summary.period_start == datetime(2026, 2, 1, tzinfo=UTC)
    assert summary.period_end == datetime(2026, 3, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_usage_summary_endpoint_is_tenant_scoped(
    client,
    organization_factory,
    user_factory,
):
    user = await user_factory()
    organization, _ = await organization_factory(owner=user)
    token, _ = create_access_token(user.id)

    response = await client.get(
        f"/organizations/{organization.id}/usage",
        headers=auth_headers(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["plan"] == "FREE"
    assert response.json()["limit"] == 1_000
    assert response.json()["used"] == 0


@pytest.mark.asyncio
async def test_api_key_rate_limit_has_retry_after(
    client,
    db_session,
    redis_client,
    organization_factory,
    user_factory,
    monkeypatch,
):
    user = await user_factory()
    organization, _ = await organization_factory(owner=user)
    raw_key = await create_test_api_key(db_session, organization, user)
    monkeypatch.setattr(get_settings(), "api_rate_limit_per_minute", 2)
    url = f"/organizations/{organization.id}/usage/events"

    first = await client.post(url, headers={"X-API-Key": raw_key}, json={"quantity": 1})
    second = await client.post(url, headers={"X-API-Key": raw_key}, json={"quantity": 1})
    limited = await client.post(url, headers={"X-API-Key": raw_key}, json={"quantity": 1})

    assert first.status_code == second.status_code == 201
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert await redis_client.dbsize() == 1


@pytest.mark.asyncio
async def test_owner_can_read_subscription(
    client,
    organization_factory,
    user_factory,
):
    owner = await user_factory()
    organization, _ = await organization_factory(owner=owner)
    token, _ = create_access_token(owner.id)

    response = await client.get(
        f"/organizations/{organization.id}/subscription",
        headers=auth_headers(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["organization_id"] == str(organization.id)
    assert response.json()["plan"] == "FREE"


@pytest.mark.asyncio
async def test_member_cannot_read_subscription(
    client,
    organization_factory,
    user_factory,
):
    member = await user_factory()
    organization, _ = await organization_factory(owner=member, role=Role.MEMBER)
    token, _ = create_access_token(member.id)

    response = await client.get(
        f"/organizations/{organization.id}/subscription",
        headers=auth_headers(token),
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_change_plan_updates_subscription_and_audit(
    client,
    db_session,
    organization_factory,
    user_factory,
):
    owner = await user_factory()
    organization, _ = await organization_factory(owner=owner)
    organization_id = organization.id
    token, _ = create_access_token(owner.id)

    response = await client.post(
        f"/organizations/{organization_id}/subscription/change-plan",
        headers=auth_headers(token),
        json={"plan": "PRO"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["plan"] == "PRO"
    db_session.expire_all()
    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.organization_id == organization_id)
    )
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.organization_id == organization_id,
            AuditLog.action == "subscription.changed",
        )
    )
    assert subscription.plan == Plan.PRO
    assert audit.details == {"from_plan": "FREE", "to_plan": "PRO"}


@pytest.mark.asyncio
async def test_change_plan_is_disabled_outside_development(
    client,
    organization_factory,
    user_factory,
):
    owner = await user_factory()
    organization, _ = await organization_factory(owner=owner)
    token, _ = create_access_token(owner.id)
    production_settings = Settings(
        app_env="production",
        jwt_secret="j" * 40,
        billing_webhook_secret="b" * 40,
        _env_file=None,
    )
    app.dependency_overrides[get_settings] = lambda: production_settings

    response = await client.post(
        f"/organizations/{organization.id}/subscription/change-plan",
        headers=auth_headers(token),
        json={"plan": "PRO"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "billing_adapter_unavailable"


@pytest.mark.asyncio
async def test_audit_log_allows_admin_but_not_member(
    client,
    db_session,
    organization_factory,
    user_factory,
):
    admin = await user_factory()
    admin_org, _ = await organization_factory(owner=admin, role=Role.ADMIN)
    await write_audit(
        db_session,
        admin_org.id,
        admin.id,
        "project.created",
        "project",
        uuid.uuid4(),
        {"name": "Visible"},
    )
    await db_session.commit()
    admin_token, _ = create_access_token(admin.id)
    allowed = await client.get(
        f"/organizations/{admin_org.id}/audit-log",
        headers=auth_headers(admin_token),
    )

    member = await user_factory()
    member_org, _ = await organization_factory(owner=member, role=Role.MEMBER)
    member_token, _ = create_access_token(member.id)
    denied = await client.get(
        f"/organizations/{member_org.id}/audit-log",
        headers=auth_headers(member_token),
    )

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()[0]["metadata"] == {"name": "Visible"}
    assert denied.status_code == 403

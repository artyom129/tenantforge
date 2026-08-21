import logging
from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import get_session
from app.main import app
from app.models.identity import User


def test_production_rejects_public_development_secrets() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            jwt_secret="tenantforge-local-jwt-secret-change-this",
            billing_webhook_secret="tenantforge-local-billing-secret-change-this",
        )


def test_production_requires_different_secrets() -> None:
    shared = "a-random-looking-but-shared-secret-value"
    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            jwt_secret=shared,
            billing_webhook_secret=shared,
        )


def test_production_accepts_distinct_long_secrets() -> None:
    settings = Settings(
        app_env="production",
        jwt_secret="jwt-secret-value-that-is-long-and-random-001",
        billing_webhook_secret="webhook-secret-value-that-is-long-random-002",
    )

    assert settings.app_env == "production"


@pytest.mark.asyncio
async def test_request_log_uses_route_template_instead_of_invitation_token(
    client,
    caplog,
) -> None:
    token = "one-time-invitation-secret-that-must-not-be-logged"
    caplog.set_level(logging.INFO, logger="app.core.logging")

    response = await client.post(f"/invitations/{token}/accept")

    assert response.status_code == 401
    application_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.core.logging"
    ]
    assert any('"path": "/invitations/{token}/accept"' in message for message in application_logs)
    assert all(token not in message for message in application_logs)


@pytest.mark.asyncio
async def test_transaction_failure_is_reported_before_success_response(
    client,
    session_factory,
) -> None:
    async def failing_transaction() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                raise RuntimeError("simulated commit failure")
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = failing_transaction
    response = await client.post(
        "/auth/register",
        json={"email": "rollback@example.test", "password": "long-enough-password"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(User).where(User.email == "rollback@example.test")
        )
    assert count == 0

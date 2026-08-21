import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import RefreshToken, User
from app.security.opaque_tokens import hash_token
from app.security.passwords import verify_password
from tests.helpers import DEFAULT_PASSWORD, auth_headers, register_and_login

pytestmark = pytest.mark.asyncio


async def test_register_normalizes_email_and_hashes_password(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "  Alice@Example.Test ", "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.test"
    assert body["is_active"] is True
    assert body["is_verified"] is False
    assert "password" not in body

    user = await db_session.scalar(select(User).where(User.email == "alice@example.test"))
    assert user is not None
    assert user.password_hash != DEFAULT_PASSWORD
    assert verify_password(DEFAULT_PASSWORD, user.password_hash)


async def test_register_rejects_invalid_input_with_validation_envelope(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "short"},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Request validation failed"
    assert len(error["details"]) == 2
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_register_rejects_duplicate_email_case_insensitively(
    client: AsyncClient,
) -> None:
    first = await client.post(
        "/auth/register",
        json={"email": "duplicate@example.test", "password": DEFAULT_PASSWORD},
    )
    second = await client.post(
        "/auth/register",
        json={"email": "DUPLICATE@example.test", "password": DEFAULT_PASSWORD},
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "email_taken"


async def test_login_returns_bearer_token_pair(client: AsyncClient) -> None:
    tokens = await register_and_login(client)

    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 15 * 60
    assert tokens["access_token"].count(".") == 2
    assert len(tokens["refresh_token"]) >= 32


async def test_login_rejects_wrong_password(client: AsyncClient) -> None:
    await client.post(
        "/auth/register",
        json={"email": "owner@example.test", "password": DEFAULT_PASSWORD},
    )

    response = await client.post(
        "/auth/login",
        json={"email": "owner@example.test", "password": "definitely-wrong"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_unknown_email_uses_generic_credentials_error(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/auth/login",
        json={"email": "missing@example.test", "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.json()["error"]["message"] == "Invalid email or password"


async def test_me_returns_authenticated_user(client: AsyncClient) -> None:
    tokens = await register_and_login(client, email="me@example.test")

    response = await client.get(
        "/auth/me",
        headers=auth_headers(tokens["access_token"]),
    )

    assert response.status_code == 200
    assert response.json()["email"] == "me@example.test"


async def test_me_requires_bearer_credentials(client: AsyncClient) -> None:
    response = await client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


async def test_me_rejects_invalid_jwt(client: AsyncClient) -> None:
    response = await client.get(
        "/auth/me",
        headers=auth_headers("not-a-signed-jwt"),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_access_token"


async def test_me_rejects_expired_jwt(client: AsyncClient) -> None:
    registration = await client.post(
        "/auth/register",
        json={"email": "expired@example.test", "password": DEFAULT_PASSWORD},
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": registration.json()["id"],
            "type": "access",
            "jti": str(uuid.uuid4()),
            "iat": now - timedelta(minutes=2),
            "exp": now - timedelta(minutes=1),
        },
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    response = await client.get("/auth/me", headers=auth_headers(token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_access_token"


async def test_refresh_rotates_token_and_revokes_previous_record(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    original = await register_and_login(client, email="rotate@example.test")

    response = await client.post(
        "/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
    )

    assert response.status_code == 200
    replacement = response.json()
    assert replacement["access_token"] != original["access_token"]
    assert replacement["refresh_token"] != original["refresh_token"]

    previous = await db_session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(original["refresh_token"])
        )
    )
    current = await db_session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(replacement["refresh_token"])
        )
    )
    assert previous is not None and previous.revoked_at is not None
    assert current is not None and current.revoked_at is None


async def test_old_refresh_token_is_rejected_after_rotation(client: AsyncClient) -> None:
    original = await register_and_login(client, email="reuse@example.test")
    rotated = await client.post(
        "/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
    )
    assert rotated.status_code == 200

    response = await client.post(
        "/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "refresh_token_reused"


async def test_logout_revokes_refresh_token(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    tokens = await register_and_login(client, email="logout@example.test")

    response = await client.post(
        "/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert response.status_code == 204
    assert response.content == b""
    stored = await db_session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(tokens["refresh_token"])
        )
    )
    assert stored is not None and stored.revoked_at is not None


async def test_logged_out_refresh_token_cannot_be_reused(client: AsyncClient) -> None:
    tokens = await register_and_login(client, email="revoked@example.test")
    logout = await client.post(
        "/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert logout.status_code == 204

    response = await client.post(
        "/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "refresh_token_reused"


async def test_logout_rejects_unknown_refresh_token(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/logout",
        json={"refresh_token": "x" * 48},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh_token"


async def test_valid_request_id_is_preserved(client: AsyncClient) -> None:
    response = await client.get(
        "/health/live",
        headers={"X-Request-ID": "request.auth-test_42"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request.auth-test_42"


async def test_invalid_request_id_is_replaced_and_used_in_error_envelope(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/auth/login",
        headers={"X-Request-ID": "not valid because spaces"},
        json={},
    )

    assert response.status_code == 422
    request_id = response.headers["X-Request-ID"]
    assert request_id.startswith("tf_")
    assert response.json()["error"]["request_id"] == request_id


async def test_application_error_has_stable_envelope(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/login",
        headers={"X-Request-ID": "stable-error-id"},
        json={"email": "missing@example.test", "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "invalid_credentials",
            "message": "Invalid email or password",
            "request_id": "stable-error-id",
        }
    }

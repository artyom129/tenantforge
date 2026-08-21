import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings


class InvalidAccessToken(ValueError):
    """Raised when an access token cannot be trusted."""


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    settings = get_settings()
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + lifetime,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> uuid.UUID:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["sub", "type", "jti", "iat", "exp"]},
        )
        if payload["type"] != "access":
            raise InvalidAccessToken("Unexpected token type")
        return uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise InvalidAccessToken("Invalid or expired access token") from exc

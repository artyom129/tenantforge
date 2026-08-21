from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import AuthenticationError, ConflictError
from app.models.identity import RefreshToken, User
from app.security.jwt import create_access_token
from app.security.opaque_tokens import generate_opaque_token, hash_token
from app.security.passwords import hash_password, verify_password

_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


async def register_user(session: AsyncSession, email: str, password: str) -> User:
    email = email.strip().lower()
    existing = await session.scalar(select(User.id).where(User.email == email))
    if existing is not None:
        raise ConflictError("email_taken", "An account with this email already exists")

    user = User(email=email, password_hash=hash_password(password))
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("email_taken", "An account with this email already exists") from exc
    await session.refresh(user)
    return user


async def authenticate_user(session: AsyncSession, email: str, password: str) -> User:
    email = email.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    candidate_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_matches = verify_password(password, candidate_hash)
    if user is None or not password_matches or not user.is_active:
        raise AuthenticationError("invalid_credentials", "Invalid email or password")
    return user


async def issue_tokens(session: AsyncSession, user: User) -> IssuedTokens:
    if not user.is_active:
        raise AuthenticationError("inactive_user", "This account is inactive")

    now = datetime.now(UTC)
    refresh_token = generate_opaque_token()
    settings = get_settings()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=now + timedelta(days=settings.refresh_token_expire_days),
            created_at=now,
        )
    )
    access_token, expires_in = create_access_token(user.id)
    await session.flush()
    return IssuedTokens(access_token, refresh_token, expires_in)


async def rotate_refresh_token(session: AsyncSession, raw_token: str) -> IssuedTokens:
    now = datetime.now(UTC)
    statement = (
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw_token))
        .with_for_update()
    )
    stored_token = await session.scalar(statement)
    if stored_token is None:
        raise AuthenticationError("invalid_refresh_token", "Invalid refresh token")
    if stored_token.revoked_at is not None:
        raise AuthenticationError("refresh_token_reused", "Refresh token has already been used")
    if _has_expired(stored_token.expires_at, now):
        raise AuthenticationError("refresh_token_expired", "Refresh token has expired")

    user = await session.scalar(select(User).where(User.id == stored_token.user_id))
    if user is None or not user.is_active:
        raise AuthenticationError("inactive_user", "This account is inactive")

    stored_token.revoked_at = now
    replacement = await issue_tokens(session, user)
    await session.flush()
    return replacement


async def revoke_refresh_token(session: AsyncSession, raw_token: str) -> None:
    statement = (
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw_token))
        .with_for_update()
    )
    stored_token = await session.scalar(statement)
    if stored_token is None:
        raise AuthenticationError("invalid_refresh_token", "Invalid refresh token")
    if stored_token.revoked_at is None:
        stored_token.revoked_at = datetime.now(UTC)
        await session.flush()


def _has_expired(expires_at: datetime, now: datetime) -> bool:
    comparable_now = now if expires_at.tzinfo is not None else now.replace(tzinfo=None)
    return expires_at <= comparable_now

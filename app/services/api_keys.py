import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError, ForbiddenError, NotFoundError
from app.models.enums import Role
from app.models.resources import ApiKey
from app.models.tenancy import Membership
from app.security.opaque_tokens import generate_api_key, hash_token
from app.services.audit import write_audit


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    key_id: uuid.UUID
    organization_id: uuid.UUID


async def create_api_key(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
) -> tuple[ApiKey, str]:
    await _lock_owner(session, organization_id, actor_id)
    raw_key, prefix = generate_api_key()
    api_key = ApiKey(
        organization_id=organization_id,
        name=name,
        key_hash=hash_token(raw_key),
        prefix=prefix,
        created_by=actor_id,
    )
    session.add(api_key)
    await session.flush()
    await session.refresh(api_key)
    await write_audit(
        session,
        organization_id,
        actor_id,
        "api_key.created",
        "api_key",
        str(api_key.id),
        {"name": name, "prefix": prefix},
    )
    return api_key, raw_key


async def list_api_keys(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> list[ApiKey]:
    statement = (
        select(ApiKey)
        .join(Membership, Membership.organization_id == ApiKey.organization_id)
        .where(
            ApiKey.organization_id == organization_id,
            Membership.user_id == actor_id,
            Membership.role == Role.OWNER,
        )
        .order_by(ApiKey.created_at.desc(), ApiKey.id)
    )
    return list((await session.scalars(statement)).all())


async def revoke_api_key(
    session: AsyncSession,
    organization_id: uuid.UUID,
    key_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    await _lock_owner(session, organization_id, actor_id)
    statement = (
        select(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.organization_id == organization_id)
        .with_for_update()
    )
    api_key = await session.scalar(statement)
    if api_key is None:
        raise NotFoundError("api_key_not_found", "API key not found")
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        await write_audit(
            session,
            organization_id,
            actor_id,
            "api_key.revoked",
            "api_key",
            str(api_key.id),
        )
        await session.flush()


async def authenticate_api_key(session: AsyncSession, raw_key: str) -> ApiKeyPrincipal:
    if not raw_key.startswith("tf_live_"):
        raise AuthenticationError("invalid_api_key", "Invalid API key")
    statement = select(ApiKey).where(
        ApiKey.key_hash == hash_token(raw_key),
        ApiKey.revoked_at.is_(None),
    )
    api_key = await session.scalar(statement)
    if api_key is None:
        raise AuthenticationError("invalid_api_key", "Invalid API key")
    api_key.last_used_at = datetime.now(UTC)
    await session.flush()
    return ApiKeyPrincipal(api_key.id, api_key.organization_id)


async def _lock_owner(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Membership:
    statement = (
        select(Membership)
        .where(
            Membership.organization_id == organization_id,
            Membership.user_id == user_id,
            Membership.role == Role.OWNER,
        )
        .with_for_update()
    )
    membership = await session.scalar(statement)
    if membership is None:
        raise ForbiddenError("owner_required", "Only an owner can manage API keys")
    return membership

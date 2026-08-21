import uuid
from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import AuthenticationError, ForbiddenError, NotFoundError
from app.database import get_session
from app.infrastructure.rate_limit import FixedWindowRateLimiter
from app.infrastructure.redis import get_redis
from app.models.enums import Role
from app.models.identity import User
from app.models.tenancy import Membership
from app.security.jwt import InvalidAccessToken, decode_access_token
from app.services.api_keys import ApiKeyPrincipal, authenticate_api_key
from app.services.email import DevelopmentEmailSender, InvitationEmailSender

bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

Session = Annotated[AsyncSession, Depends(get_session, scope="function")]
DbSession = Session
RedisClient = Annotated[Redis, Depends(get_redis)]


def get_invitation_email_sender(redis: RedisClient) -> InvitationEmailSender:
    return DevelopmentEmailSender(redis)


CurrentInvitationEmailSender = Annotated[
    InvitationEmailSender,
    Depends(get_invitation_email_sender),
]


async def get_current_user(
    session: Session,
    redis: RedisClient,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError()
    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidAccessToken as exc:
        raise AuthenticationError("invalid_access_token", str(exc)) from exc

    user = await session.scalar(
        select(User).where(
            User.id == user_id,
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise AuthenticationError("invalid_access_token", "Invalid or expired access token")
    await FixedWindowRateLimiter(redis).enforce(
        f"user:{user.id}",
        get_settings().user_rate_limit_per_minute,
    )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_membership(
    organization_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
) -> Membership:
    statement = select(Membership).where(
        Membership.organization_id == organization_id,
        Membership.user_id == current_user.id,
    )
    membership = await session.scalar(statement)
    if membership is None:
        raise NotFoundError("organization_not_found", "Organization not found")
    return membership


CurrentMembership = Annotated[Membership, Depends(get_current_membership)]
OrganizationMembership = CurrentMembership


def require_roles(*allowed_roles: Role):
    async def check_role(membership: CurrentMembership) -> Membership:
        if membership.role not in allowed_roles:
            raise ForbiddenError()
        return membership

    return check_role


OwnerMembership = Annotated[Membership, Depends(require_roles(Role.OWNER))]
OwnerOrAdminMembership = Annotated[
    Membership,
    Depends(require_roles(Role.OWNER, Role.ADMIN)),
]


async def get_api_key_principal(
    session: Session,
    redis: RedisClient,
    raw_key: Annotated[str | None, Security(api_key_header)],
) -> ApiKeyPrincipal:
    if raw_key is None or not 8 <= len(raw_key) <= 256:
        raise AuthenticationError("invalid_api_key", "Invalid API key")
    principal = await authenticate_api_key(session, raw_key)
    await FixedWindowRateLimiter(redis).enforce(
        f"api-key:{principal.key_id}",
        get_settings().api_rate_limit_per_minute,
    )
    return principal


CurrentApiKey = Annotated[ApiKeyPrincipal, Depends(get_api_key_principal)]

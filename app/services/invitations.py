import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.enums import Role
from app.models.identity import User
from app.models.tenancy import Invitation, Membership, Organization
from app.schemas.invitations import InvitationCreate
from app.security.opaque_tokens import generate_opaque_token, hash_token
from app.security.permissions import ensure_invitation_role
from app.services.audit import write_audit


async def create_invitation(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    data: InvitationCreate,
) -> tuple[Invitation, str]:
    await _lock_organization(session, organization_id)
    actor_membership = await _lock_actor(session, organization_id, actor_id)
    ensure_invitation_role(actor_membership.role, data.role)
    now = datetime.now(UTC)

    member_exists = await session.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.organization_id == organization_id,
            func.lower(User.email) == data.email,
        )
    )
    if member_exists is not None:
        raise ConflictError("already_member", "This user is already a member")

    active_invitation = await session.scalar(
        select(Invitation.id).where(
            Invitation.organization_id == organization_id,
            func.lower(Invitation.email) == data.email,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > now,
        )
    )
    if active_invitation is not None:
        raise ConflictError("invitation_exists", "An active invitation already exists")

    raw_token = generate_opaque_token()
    invitation = Invitation(
        organization_id=organization_id,
        email=data.email,
        role=data.role,
        token_hash=hash_token(raw_token),
        expires_at=now + timedelta(days=get_settings().invitation_expire_days),
        created_by=actor_id,
        created_at=now,
    )
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation)
    await write_audit(
        session,
        organization_id,
        actor_id,
        "member.invited",
        "invitation",
        str(invitation.id),
        {"email": data.email, "role": data.role.value},
    )
    return invitation, raw_token


async def accept_invitation(
    session: AsyncSession,
    raw_token: str,
    user: User,
) -> Membership:
    now = datetime.now(UTC)
    statement = (
        select(Invitation).where(Invitation.token_hash == hash_token(raw_token)).with_for_update()
    )
    invitation = await session.scalar(statement)
    if invitation is None:
        raise NotFoundError("invitation_not_found", "Invitation not found")
    if invitation.accepted_at is not None:
        raise ConflictError("invitation_used", "Invitation has already been accepted")
    if _has_expired(invitation.expires_at, now):
        raise ConflictError("invitation_expired", "Invitation has expired")
    if invitation.email.lower() != user.email.lower():
        raise ForbiddenError(
            "invitation_email_mismatch",
            "This invitation belongs to another account",
        )

    existing = await session.scalar(
        select(Membership.id).where(
            Membership.organization_id == invitation.organization_id,
            Membership.user_id == user.id,
        )
    )
    if existing is not None:
        raise ConflictError("already_member", "This user is already a member")

    membership = Membership(
        user_id=user.id,
        organization_id=invitation.organization_id,
        role=invitation.role,
    )
    invitation.accepted_at = now
    session.add(membership)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("already_member", "This user is already a member") from exc
    await session.refresh(membership)
    await write_audit(
        session,
        invitation.organization_id,
        user.id,
        "invitation.accepted",
        "invitation",
        str(invitation.id),
        {"membership_id": str(membership.id)},
    )
    return membership


async def _lock_organization(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> None:
    organization = await session.scalar(
        select(Organization.id)
        .where(Organization.id == organization_id)
        .with_for_update()
    )
    if organization is None:
        raise ForbiddenError()


async def _lock_actor(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Membership:
    statement = (
        select(Membership)
        .where(
            Membership.organization_id == organization_id,
            Membership.user_id == user_id,
            Membership.role.in_((Role.OWNER, Role.ADMIN)),
        )
        .with_for_update()
    )
    membership = await session.scalar(statement)
    if membership is None:
        raise ForbiddenError()
    return membership


def _has_expired(expires_at: datetime, now: datetime) -> bool:
    comparable_now = now if expires_at.tzinfo is not None else now.replace(tzinfo=None)
    return expires_at <= comparable_now

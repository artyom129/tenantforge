import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.enums import Role
from app.models.identity import User
from app.models.tenancy import Membership, Organization
from app.schemas.organizations import OrganizationCreate, OrganizationUpdate
from app.services.audit import write_audit


async def create_organization(
    session: AsyncSession,
    actor: User,
    data: OrganizationCreate,
) -> Organization:
    slug = data.slug.lower()
    if await session.scalar(select(Organization.id).where(Organization.slug == slug)) is not None:
        raise ConflictError("slug_taken", "This organization slug is already in use")

    organization = Organization(name=data.name, slug=slug)
    session.add(organization)
    try:
        await session.flush()
        membership = Membership(
            user_id=actor.id,
            organization_id=organization.id,
            role=Role.OWNER,
        )
        session.add(membership)
        await session.flush()
        from app.services.billing import create_default_subscription

        await create_default_subscription(session, organization.id)
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("slug_taken", "This organization slug is already in use") from exc

    await session.refresh(organization)
    await write_audit(
        session,
        organization.id,
        actor.id,
        "organization.created",
        "organization",
        str(organization.id),
    )
    return organization


async def list_organizations(session: AsyncSession, user_id: uuid.UUID) -> list[Organization]:
    statement = (
        select(Organization)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.created_at, Organization.id)
    )
    return list((await session.scalars(statement)).all())


async def get_organization(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Organization:
    statement = (
        select(Organization)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(
            Organization.id == organization_id,
            Membership.user_id == user_id,
        )
    )
    organization = await session.scalar(statement)
    if organization is None:
        raise NotFoundError("organization_not_found", "Organization not found")
    return organization


async def update_organization(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    data: OrganizationUpdate,
) -> Organization:
    statement = (
        select(Organization)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(
            Organization.id == organization_id,
            Membership.user_id == actor_id,
            Membership.role.in_((Role.OWNER, Role.ADMIN)),
        )
        .with_for_update()
    )
    organization = await session.scalar(statement)
    if organization is None:
        raise ForbiddenError()

    changes = data.model_dump(exclude_unset=True)
    if "slug" in changes:
        changes["slug"] = changes["slug"].lower()
    for field, value in changes.items():
        setattr(organization, field, value)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("slug_taken", "This organization slug is already in use") from exc
    await session.refresh(organization)
    await write_audit(
        session,
        organization_id,
        actor_id,
        "organization.updated",
        "organization",
        str(organization_id),
        {"fields": sorted(changes)},
    )
    return organization


async def delete_organization(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    statement = (
        select(Organization)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(
            Organization.id == organization_id,
            Membership.user_id == actor_id,
            Membership.role == Role.OWNER,
        )
        .with_for_update()
    )
    organization = await session.scalar(statement)
    if organization is None:
        raise ForbiddenError("owner_required", "Only an owner can delete the organization")
    await write_audit(
        session,
        organization_id,
        actor_id,
        "organization.deleted",
        "organization",
        str(organization_id),
    )
    await session.delete(organization)
    await session.flush()


async def list_members(session: AsyncSession, organization_id: uuid.UUID) -> list[dict]:
    statement = (
        select(Membership, User.email)
        .join(User, User.id == Membership.user_id)
        .where(Membership.organization_id == organization_id)
        .order_by(Membership.joined_at, Membership.id)
    )
    rows = (await session.execute(statement)).all()
    return [
        {
            "id": membership.id,
            "user_id": membership.user_id,
            "organization_id": membership.organization_id,
            "role": membership.role,
            "joined_at": membership.joined_at,
            "email": email,
        }
        for membership, email in rows
    ]


async def update_membership(
    session: AsyncSession,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    actor_id: uuid.UUID,
    new_role: Role,
) -> Membership:
    actor = await _lock_actor_membership(session, organization_id, actor_id)
    target = await _lock_membership(session, organization_id, membership_id)

    if actor.role == Role.ADMIN:
        if target.role != Role.MEMBER or new_role != Role.MEMBER:
            raise ForbiddenError(
                "membership_change_forbidden",
                "Administrators cannot grant roles or manage owners",
            )
    elif actor.role != Role.OWNER:
        raise ForbiddenError()

    if target.role == Role.OWNER and new_role != Role.OWNER:
        await _ensure_another_owner(session, organization_id, target.id)

    old_role = target.role
    target.role = new_role
    await session.flush()
    await write_audit(
        session,
        organization_id,
        actor_id,
        "member.role_changed",
        "membership",
        str(target.id),
        {"old_role": old_role.value, "new_role": new_role.value},
    )
    return target


async def remove_membership(
    session: AsyncSession,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    actor = await _lock_actor_membership(session, organization_id, actor_id)
    target = await _lock_membership(session, organization_id, membership_id)

    if actor.role == Role.ADMIN and target.role != Role.MEMBER:
        raise ForbiddenError(
            "membership_remove_forbidden",
            "Administrators can remove members only",
        )
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise ForbiddenError()
    if target.role == Role.OWNER:
        await _ensure_another_owner(session, organization_id, target.id)

    await write_audit(
        session,
        organization_id,
        actor_id,
        "member.removed",
        "membership",
        str(target.id),
        {"user_id": str(target.user_id), "role": target.role.value},
    )
    await session.delete(target)
    await session.flush()


async def _lock_actor_membership(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Membership:
    statement = (
        select(Membership)
        .where(
            Membership.organization_id == organization_id,
            Membership.user_id == user_id,
        )
        .with_for_update()
    )
    membership = await session.scalar(statement)
    if membership is None:
        raise ForbiddenError()
    return membership


async def _lock_membership(
    session: AsyncSession,
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
) -> Membership:
    statement = (
        select(Membership)
        .where(
            Membership.id == membership_id,
            Membership.organization_id == organization_id,
        )
        .with_for_update()
    )
    membership = await session.scalar(statement)
    if membership is None:
        raise NotFoundError("membership_not_found", "Membership not found")
    return membership


async def _ensure_another_owner(
    session: AsyncSession,
    organization_id: uuid.UUID,
    excluded_membership_id: uuid.UUID,
) -> None:
    statement = (
        select(Membership.id)
        .where(
            Membership.organization_id == organization_id,
            Membership.role == Role.OWNER,
        )
        .with_for_update()
    )
    owner_ids = list((await session.scalars(statement)).all())
    if not any(owner_id != excluded_membership_id for owner_id in owner_ids):
        raise ConflictError(
            "last_owner",
            "An organization must keep at least one owner",
        )

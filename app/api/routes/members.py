import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, OrganizationMembership, OwnerOrAdminMembership, Session
from app.schemas.organizations import MemberRead, MembershipRead, MembershipUpdate
from app.services.organizations import list_members, remove_membership, update_membership

router = APIRouter(prefix="/organizations/{organization_id}/members", tags=["Members"])


@router.get("", response_model=list[MemberRead])
async def list_all(
    organization_id: uuid.UUID,
    session: Session,
    _membership: OrganizationMembership,
) -> list[MemberRead]:
    members = await list_members(session, organization_id)
    return [MemberRead.model_validate(member) for member in members]


@router.patch("/{membership_id}", response_model=MembershipRead)
async def update(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    payload: MembershipUpdate,
    session: Session,
    current_user: CurrentUser,
    _membership: OwnerOrAdminMembership,
) -> MembershipRead:
    membership = await update_membership(
        session,
        organization_id,
        membership_id,
        current_user.id,
        payload.role,
    )
    return MembershipRead.model_validate(membership)


@router.delete("/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    organization_id: uuid.UUID,
    membership_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OwnerOrAdminMembership,
) -> Response:
    await remove_membership(session, organization_id, membership_id, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

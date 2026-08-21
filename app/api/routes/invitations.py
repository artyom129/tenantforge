import uuid

import structlog
from fastapi import APIRouter, status

from app.api.deps import (
    CurrentInvitationEmailSender,
    CurrentUser,
    OwnerOrAdminMembership,
    Session,
)
from app.schemas.invitations import InvitationCreate, InvitationCreated
from app.schemas.organizations import MembershipRead
from app.services.email import InvitationDeliveryError
from app.services.invitations import accept_invitation, create_invitation

router = APIRouter(tags=["Invitations"])


@router.post(
    "/organizations/{organization_id}/invitations",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create(
    organization_id: uuid.UUID,
    payload: InvitationCreate,
    session: Session,
    email_sender: CurrentInvitationEmailSender,
    current_user: CurrentUser,
    _membership: OwnerOrAdminMembership,
) -> InvitationCreated:
    invitation, raw_token = await create_invitation(
        session,
        organization_id,
        current_user.id,
        payload,
    )
    await session.commit()
    try:
        await email_sender.send_invitation(
            email=invitation.email,
            organization_id=invitation.organization_id,
            invitation_id=invitation.id,
            token=raw_token,
        )
    except InvitationDeliveryError:
        structlog.get_logger().error(
            "invitation_email_delivery_failed",
            invitation_id=str(invitation.id),
            organization_id=str(invitation.organization_id),
        )
    return InvitationCreated(
        id=invitation.id,
        organization_id=invitation.organization_id,
        email=invitation.email,
        role=invitation.role,
        token=raw_token,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
    )


@router.post("/invitations/{token}/accept", response_model=MembershipRead)
async def accept(token: str, session: Session, current_user: CurrentUser) -> MembershipRead:
    membership = await accept_invitation(session, token, current_user)
    return MembershipRead.model_validate(membership)

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.database import get_session
from app.models.enums import Role
from app.models.tenancy import Membership
from app.schemas.billing import ChangePlanRequest, SubscriptionRead
from app.services.billing import change_plan, get_subscription

router = APIRouter(prefix="/organizations/{organization_id}/subscription", tags=["Billing"])
OwnerMembership = Annotated[Membership, Depends(require_roles(Role.OWNER))]
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]


def require_development_billing(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Settings:
    if settings.app_env.lower() not in {"development", "test"}:
        raise NotFoundError(
            "billing_adapter_unavailable",
            "Development billing adapter is unavailable",
        )
    return settings


DevelopmentBilling = Annotated[Settings, Depends(require_development_billing)]


@router.get("", response_model=SubscriptionRead)
async def read_subscription(
    organization_id: uuid.UUID,
    membership: OwnerMembership,
    session: Session,
) -> SubscriptionRead:
    subscription = await get_subscription(session, membership.organization_id)
    return SubscriptionRead.model_validate(subscription)


@router.post("/change-plan", response_model=SubscriptionRead)
async def update_plan(
    organization_id: uuid.UUID,
    payload: ChangePlanRequest,
    request: Request,
    membership: OwnerMembership,
    session: Session,
    _billing_environment: DevelopmentBilling,
) -> SubscriptionRead:
    ip_address = request.client.host if request.client is not None else None
    subscription = await change_plan(
        session,
        membership.organization_id,
        payload.plan,
        actor_user_id=membership.user_id,
        ip_address=ip_address,
    )
    return SubscriptionRead.model_validate(subscription)

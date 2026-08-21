import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentApiKey, get_current_membership
from app.core.errors import NotFoundError
from app.database import get_session
from app.models.tenancy import Membership
from app.schemas.usage import UsageEventCreate, UsageEventRead, UsageSummary
from app.services.usage import get_usage_summary, record_usage

router = APIRouter(prefix="/organizations/{organization_id}/usage", tags=["Usage"])
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]


@router.get("", response_model=UsageSummary)
async def read_usage(
    organization_id: uuid.UUID,
    membership: Annotated[Membership, Depends(get_current_membership)],
    session: Session,
) -> UsageSummary:
    summary = await get_usage_summary(session, membership.organization_id)
    return UsageSummary.model_validate(summary)


@router.post("/events", response_model=UsageEventRead, status_code=status.HTTP_201_CREATED)
async def create_usage_event(
    organization_id: uuid.UUID,
    payload: UsageEventCreate,
    principal: CurrentApiKey,
    session: Session,
) -> UsageEventRead:
    if principal.organization_id != organization_id:
        raise NotFoundError("organization_not_found", "Organization not found")
    event = await record_usage(
        session,
        principal.organization_id,
        quantity=payload.quantity,
    )
    return UsageEventRead.model_validate(event)

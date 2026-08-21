import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.database import get_session
from app.models.enums import Role
from app.models.tenancy import Membership
from app.schemas.audit import AuditLogRead
from app.services.audit import list_audit_logs

router = APIRouter(prefix="/organizations/{organization_id}/audit-log", tags=["Audit"])
AuditMembership = Annotated[
    Membership,
    Depends(require_roles(Role.OWNER, Role.ADMIN)),
]
Session = Annotated[AsyncSession, Depends(get_session, scope="function")]


@router.get("", response_model=list[AuditLogRead])
async def read_audit_log(
    organization_id: uuid.UUID,
    membership: AuditMembership,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogRead]:
    entries = await list_audit_logs(
        session,
        membership.organization_id,
        limit=limit,
        offset=offset,
    )
    return [AuditLogRead.model_validate(entry) for entry in entries]

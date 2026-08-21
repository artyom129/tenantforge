import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


async def write_audit(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        details=metadata or {},
        ip_address=ip_address,
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_audit_logs(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditLog]:
    result = await session.scalars(
        select(AuditLog)
        .where(AuditLog.organization_id == organization_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result)


import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.models.resources import Project
from app.models.tenancy import Membership
from app.schemas.projects import ProjectCreate, ProjectUpdate
from app.services.audit import write_audit


def _is_member(organization_id: uuid.UUID, user_id: uuid.UUID):
    return exists(
        select(Membership.id).where(
            Membership.organization_id == organization_id,
            Membership.user_id == user_id,
        )
    )


async def create_project(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    data: ProjectCreate,
) -> Project:
    if not await session.scalar(select(_is_member(organization_id, actor_id))):
        raise ForbiddenError()
    project = Project(
        organization_id=organization_id,
        name=data.name,
        description=data.description,
        created_by=actor_id,
    )
    session.add(project)
    await session.flush()
    await session.refresh(project)
    await write_audit(
        session,
        organization_id,
        actor_id,
        "project.created",
        "project",
        str(project.id),
    )
    return project


async def list_projects(
    session: AsyncSession,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> list[Project]:
    statement = (
        select(Project)
        .where(
            Project.organization_id == organization_id,
            _is_member(organization_id, actor_id),
        )
        .order_by(Project.created_at, Project.id)
    )
    return list((await session.scalars(statement)).all())


async def get_project(
    session: AsyncSession,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> Project:
    statement = select(Project).where(
        Project.id == project_id,
        Project.organization_id == organization_id,
        _is_member(organization_id, actor_id),
    )
    project = await session.scalar(statement)
    if project is None:
        raise NotFoundError("project_not_found", "Project not found")
    return project


async def update_project(
    session: AsyncSession,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    data: ProjectUpdate,
) -> Project:
    statement = (
        select(Project)
        .where(
            Project.id == project_id,
            Project.organization_id == organization_id,
            _is_member(organization_id, actor_id),
        )
        .with_for_update()
    )
    project = await session.scalar(statement)
    if project is None:
        raise NotFoundError("project_not_found", "Project not found")
    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(project, field, value)
    await session.flush()
    await session.refresh(project)
    await write_audit(
        session,
        organization_id,
        actor_id,
        "project.updated",
        "project",
        str(project.id),
        {"fields": sorted(changes)},
    )
    return project


async def delete_project(
    session: AsyncSession,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    statement = (
        select(Project)
        .where(
            Project.id == project_id,
            Project.organization_id == organization_id,
            _is_member(organization_id, actor_id),
        )
        .with_for_update()
    )
    project = await session.scalar(statement)
    if project is None:
        raise NotFoundError("project_not_found", "Project not found")
    await write_audit(
        session,
        organization_id,
        actor_id,
        "project.deleted",
        "project",
        str(project.id),
    )
    await session.delete(project)
    await session.flush()

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, OrganizationMembership, Session
from app.schemas.projects import ProjectCreate, ProjectRead, ProjectUpdate
from app.services.projects import (
    create_project,
    delete_project,
    get_project,
    list_projects,
    update_project,
)

router = APIRouter(prefix="/organizations/{organization_id}/projects", tags=["Projects"])


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create(
    organization_id: uuid.UUID,
    payload: ProjectCreate,
    session: Session,
    current_user: CurrentUser,
    _membership: OrganizationMembership,
) -> ProjectRead:
    project = await create_project(session, organization_id, current_user.id, payload)
    return ProjectRead.model_validate(project)


@router.get("", response_model=list[ProjectRead])
async def list_all(
    organization_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OrganizationMembership,
) -> list[ProjectRead]:
    projects = await list_projects(session, organization_id, current_user.id)
    return [ProjectRead.model_validate(item) for item in projects]


@router.get("/{project_id}", response_model=ProjectRead)
async def get_one(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OrganizationMembership,
) -> ProjectRead:
    project = await get_project(session, organization_id, project_id, current_user.id)
    return ProjectRead.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: Session,
    current_user: CurrentUser,
    _membership: OrganizationMembership,
) -> ProjectRead:
    project = await update_project(session, organization_id, project_id, current_user.id, payload)
    return ProjectRead.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OrganizationMembership,
) -> Response:
    await delete_project(session, organization_id, project_id, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import (
    CurrentUser,
    OrganizationMembership,
    OwnerMembership,
    OwnerOrAdminMembership,
    Session,
)
from app.schemas.organizations import OrganizationCreate, OrganizationRead, OrganizationUpdate
from app.services.organizations import (
    create_organization,
    delete_organization,
    get_organization,
    list_organizations,
    update_organization,
)

router = APIRouter(prefix="/organizations", tags=["Organizations"])


@router.post("", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
async def create(
    payload: OrganizationCreate,
    session: Session,
    current_user: CurrentUser,
) -> OrganizationRead:
    organization = await create_organization(session, current_user, payload)
    return OrganizationRead.model_validate(organization)


@router.get("", response_model=list[OrganizationRead])
async def list_all(session: Session, current_user: CurrentUser) -> list[OrganizationRead]:
    organizations = await list_organizations(session, current_user.id)
    return [OrganizationRead.model_validate(item) for item in organizations]


@router.get("/{organization_id}", response_model=OrganizationRead)
async def get_one(
    organization_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OrganizationMembership,
) -> OrganizationRead:
    organization = await get_organization(session, organization_id, current_user.id)
    return OrganizationRead.model_validate(organization)


@router.patch("/{organization_id}", response_model=OrganizationRead)
async def update(
    organization_id: uuid.UUID,
    payload: OrganizationUpdate,
    session: Session,
    current_user: CurrentUser,
    _membership: OwnerOrAdminMembership,
) -> OrganizationRead:
    organization = await update_organization(session, organization_id, current_user.id, payload)
    return OrganizationRead.model_validate(organization)


@router.delete("/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    organization_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OwnerMembership,
) -> Response:
    await delete_organization(session, organization_id, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

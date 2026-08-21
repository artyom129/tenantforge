import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, OwnerMembership, Session
from app.schemas.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyRead
from app.services.api_keys import create_api_key, list_api_keys, revoke_api_key

router = APIRouter(prefix="/organizations/{organization_id}/api-keys", tags=["API Keys"])


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create(
    organization_id: uuid.UUID,
    payload: ApiKeyCreate,
    session: Session,
    current_user: CurrentUser,
    _membership: OwnerMembership,
) -> ApiKeyCreated:
    api_key, raw_key = await create_api_key(
        session,
        organization_id,
        current_user.id,
        payload.name,
    )
    fields = ApiKeyRead.model_validate(api_key).model_dump()
    return ApiKeyCreated(**fields, key=raw_key)


@router.get("", response_model=list[ApiKeyRead])
async def list_all(
    organization_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OwnerMembership,
) -> list[ApiKeyRead]:
    api_keys = await list_api_keys(session, organization_id, current_user.id)
    return [ApiKeyRead.model_validate(item) for item in api_keys]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(
    organization_id: uuid.UUID,
    key_id: uuid.UUID,
    session: Session,
    current_user: CurrentUser,
    _membership: OwnerMembership,
) -> Response:
    await revoke_api_key(session, organization_id, key_id, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

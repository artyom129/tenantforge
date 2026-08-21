import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import Role
from app.schemas.users import normalize_email


class InvitationCreate(BaseModel):
    email: str
    role: Role = Role.MEMBER

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class InvitationCreated(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    role: Role
    token: str
    expires_at: datetime
    created_at: datetime

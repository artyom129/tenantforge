import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    prefix: str
    created_by: uuid.UUID
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyRead):
    key: str

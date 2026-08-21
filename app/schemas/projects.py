import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import ProjectStatus


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=10_000)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=10_000)
    status: ProjectStatus | None = None

    @field_validator("name", "status")
    @classmethod
    def reject_null_required_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self) -> "ProjectUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    status: ProjectStatus
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime

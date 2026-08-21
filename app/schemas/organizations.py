import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import Role


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    @field_validator("name", "slug")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(
        default=None,
        min_length=2,
        max_length=80,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )

    @field_validator("name", "slug")
    @classmethod
    def strip_text(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Field cannot be null")
        return value.strip()

    @model_validator(mode="after")
    def require_change(self) -> "OrganizationUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class MembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    joined_at: datetime


class MemberRead(MembershipRead):
    email: str


class MembershipUpdate(BaseModel):
    role: Role

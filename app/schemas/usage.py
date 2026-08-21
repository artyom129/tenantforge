import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Plan


class UsageSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan: Plan
    used: int
    limit: int
    remaining: int
    period_start: datetime
    period_end: datetime


class UsageEventCreate(BaseModel):
    quantity: int = Field(ge=1, le=1_000)


class UsageEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    metric: str
    quantity: int
    created_at: datetime

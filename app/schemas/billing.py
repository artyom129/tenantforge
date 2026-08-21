import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Plan, SubscriptionStatus, WebhookStatus


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    plan: Plan
    status: SubscriptionStatus
    external_customer_id: str | None
    external_subscription_id: str | None
    current_period_start: datetime
    current_period_end: datetime
    created_at: datetime
    updated_at: datetime


class ChangePlanRequest(BaseModel):
    plan: Plan


class BillingWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    type: str = Field(min_length=1, max_length=80)
    data: dict[str, Any]


class WebhookReceipt(BaseModel):
    event_id: uuid.UUID
    status: WebhookStatus
    duplicate: bool

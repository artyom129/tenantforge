import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import Timestamped, UUIDPrimaryKey
from app.models.enums import Plan, SubscriptionStatus, WebhookStatus


class Subscription(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "subscriptions"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    plan: Mapped[Plan] = mapped_column(
        Enum(Plan, native_enum=False), default=Plan.FREE, nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
    )
    external_customer_id: Mapped[str | None] = mapped_column(String(120))
    external_subscription_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageEvent(UUIDPrimaryKey, Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_org_metric_created", "organization_id", "metric", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WebhookEvent(UUIDPrimaryKey, Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_event_id", name="uq_webhook_provider_event"),
    )

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[WebhookStatus] = mapped_column(
        Enum(WebhookStatus, native_enum=False), default=WebhookStatus.PENDING, nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

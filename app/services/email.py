import uuid
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.infrastructure.queue import RedisJobQueue


class InvitationDeliveryError(RuntimeError):
    """Raised when an invitation cannot be handed to its delivery provider."""


class InvitationEmailSender(Protocol):
    async def send_invitation(
        self,
        *,
        email: str,
        organization_id: uuid.UUID,
        invitation_id: uuid.UUID,
        token: str,
    ) -> None:
        """Deliver an invitation or raise InvitationDeliveryError."""


class DevelopmentEmailSender:
    def __init__(self, redis: Redis) -> None:
        self.queue = RedisJobQueue(redis)

    async def send_invitation(
        self,
        *,
        email: str,
        organization_id: uuid.UUID,
        invitation_id: uuid.UUID,
        token: str,
    ) -> None:
        if not token:
            raise ValueError("invitation token is required")
        try:
            await self.queue.enqueue(
                "send_invitation_email",
                {
                    "email": email,
                    "organization_id": str(organization_id),
                    "invitation_id": str(invitation_id),
                },
            )
        except RedisError as exc:
            raise InvitationDeliveryError("Invitation delivery is unavailable") from exc

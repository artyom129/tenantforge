import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    local, separator, domain = email.rpartition("@")
    if (
        not separator
        or not local
        or not domain
        or "." not in domain
        or email.startswith(".")
        or len(email) > 320
    ):
        raise ValueError("Enter a valid email address")
    return email


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime

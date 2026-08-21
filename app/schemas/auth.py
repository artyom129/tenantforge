from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.users import normalize_email

Password = Annotated[str, Field(min_length=8, max_length=128)]


class RegisterRequest(BaseModel):
    email: str
    password: Password

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class LogoutRequest(RefreshRequest):
    model_config = ConfigDict(title="LogoutRequest")


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int

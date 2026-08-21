from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "TenantForge"
    app_env: str = "development"
    database_url: str = (
        "postgresql+asyncpg://tenantforge:tenantforge@localhost:5432/tenantforge"
    )
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: SecretStr = SecretStr("change-me-in-development-at-least-32-bytes")
    billing_webhook_secret: SecretStr = SecretStr("change-me-in-development")
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    invitation_expire_days: int = 7
    user_rate_limit_per_minute: int = 100
    api_rate_limit_per_minute: int = 60
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def reject_default_secrets_in_production(self) -> "Settings":
        if self.app_env.lower() == "production":
            jwt_secret = self.jwt_secret.get_secret_value()
            webhook_secret = self.billing_webhook_secret.get_secret_value()
            secrets = (jwt_secret, webhook_secret)
            if any(
                len(value) < 32
                or value.startswith("change-me")
                or value.startswith("tenantforge-local-")
                for value in secrets
            ):
                raise ValueError("Production secrets must be unique, random values of 32+ bytes")
            if jwt_secret == webhook_secret:
                raise ValueError("JWT and billing webhook secrets must be different")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

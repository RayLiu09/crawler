from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_driver: Literal["postgresql"] = "postgresql"
    postgres_host: str
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_database: str
    postgres_username: str
    postgres_password: SecretStr
    postgres_sslmode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=0, ge=0, le=100)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    database_connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    app_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    tenant_allowlist_path: Path = Path("/run/secrets/tenant-allowlist.yml")
    api_access_token_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    agent_session_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    lease_seconds: int = Field(default=120, ge=30, le=3600)
    lease_renewal_grace_seconds: int = Field(default=30, ge=5, le=300)
    agent_offline_seconds: int = Field(default=180, ge=60, le=7200)
    api_token_hash_key: SecretStr = Field(min_length=32)
    agent_device_credential_hash_key: SecretStr = Field(min_length=32)
    agent_token_hash_key: SecretStr = Field(min_length=32)
    callback_token_encryption_key: SecretStr = Field(min_length=43, max_length=43)
    callback_token_fingerprint_key: SecretStr = Field(min_length=32)
    callback_token_encryption_key_version: str = Field(
        default="v1",
        min_length=1,
        max_length=40,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,39}$",
    )
    webhook_dispatcher_enabled: bool = True
    webhook_dispatcher_concurrency: int = Field(default=4, ge=1, le=32)
    webhook_dispatcher_poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    webhook_request_timeout_seconds: float = Field(default=10.0, ge=1, le=60)
    webhook_claim_lease_seconds: int = Field(default=30, ge=5, le=300)
    webhook_max_attempts: int = Field(default=8, ge=1, le=50)
    webhook_retry_base_seconds: int = Field(default=5, ge=1, le=300)
    webhook_retry_max_seconds: int = Field(default=900, ge=1, le=86400)
    webhook_retry_after_max_seconds: int = Field(default=900, ge=1, le=86400)
    webhook_metrics_enabled: bool = True

    @model_validator(mode="after")
    def validate_webhook_dispatcher_timing(self) -> Self:
        if self.webhook_claim_lease_seconds <= self.webhook_request_timeout_seconds:
            raise ValueError("webhook claim lease must exceed the HTTP request timeout")
        if self.webhook_retry_base_seconds > self.webhook_retry_max_seconds:
            raise ValueError("webhook retry base cannot exceed the retry maximum")
        return self

    @property
    def database_url(self) -> URL:
        """Build an encoded async SQLAlchemy URL from injected PostgreSQL settings."""
        return URL.create(
            drivername=f"{self.postgres_driver}+asyncpg",
            username=self.postgres_username,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_database,
        )

    @property
    def database_connect_args(self) -> dict[str, object]:
        return {
            "ssl": self.postgres_sslmode,
            "timeout": self.database_connect_timeout_seconds,
            "server_settings": {"timezone": self.app_timezone},
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

import argparse
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

import keyring
from crawler_contracts.common import SourceCode
from keyring.errors import KeyringError
from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    crawler_api_url: HttpUrl
    agent_id: UUID
    agent_device_secret: SecretStr | None = None
    agent_device_secret_keyring_service: str | None = Field(
        default=None, min_length=1, max_length=120
    )
    agent_device_secret_keyring_username: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    agent_data_dir: Path
    browser_profile_root: Path
    agent_heartbeat_seconds: int = Field(default=20, ge=5, le=300)
    task_lease_renew_seconds: int = Field(default=30, ge=5, le=300)
    browser_name: Literal["auto", "chrome", "edge"] = "chrome"
    browser_profile_key: str = Field(
        default="zhaopin-primary",
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    supported_sources: list[SourceCode] = Field(default_factory=lambda: [SourceCode.ZHAOPIN])
    claim_poll_seconds: float = Field(default=5, gt=0, le=60)
    profile_lock_timeout_seconds: float = Field(default=5, ge=0, le=300)
    browser_shutdown_timeout_seconds: float = Field(default=10, gt=0, le=120)
    zhaopin_max_page_limit: int = Field(default=100, ge=1, le=100)
    shutdown_marker_path: Path | None = None
    spool_batch_size: int = Field(default=100, ge=1, le=200)
    transport_max_attempts: int = Field(default=3, ge=1, le=8)
    transport_retry_base_seconds: float = Field(default=0.5, gt=0, le=30)

    @model_validator(mode="after")
    def resolve_device_secret(self) -> "AgentSettings":
        service = self.agent_device_secret_keyring_service
        username = self.agent_device_secret_keyring_username
        if (service is None) != (username is None):
            raise ValueError("both Agent device keyring fields must be configured")
        if username is not None and username != str(self.agent_id):
            raise ValueError("Agent device keyring username must equal agentId")
        if self.agent_device_secret is not None and service is not None:
            raise ValueError("configure only one Agent device credential source")
        if self.agent_device_secret is None and service is None:
            raise ValueError("an Agent device credential source is required")
        if self.agent_device_secret is None:
            assert service is not None and username is not None
            try:
                secret = keyring.get_password(service, username)
            except KeyringError as error:
                raise ValueError("Agent device credential store is unavailable") from error
            if not secret:
                raise ValueError("Agent device credential was not found in the credential store")
            self.agent_device_secret = SecretStr(secret)
        if self.shutdown_marker_path is not None:
            marker = self.shutdown_marker_path.expanduser()
            if not marker.is_absolute():
                raise ValueError("shutdown marker path must be absolute")
            self.shutdown_marker_path = marker
        return self

    @property
    def device_secret(self) -> SecretStr:
        if self.agent_device_secret is None:
            raise RuntimeError("Agent device credential was not resolved")
        return self.agent_device_secret


def load_agent_settings(config_file: Path | None = None) -> AgentSettings:
    if config_file is None:
        return AgentSettings()  # type: ignore[call-arg]
    resolved = config_file.expanduser()
    if not resolved.is_absolute():
        raise ValueError("Agent configuration file path must be absolute")
    if not resolved.is_file():
        raise ValueError("Agent configuration file does not exist")
    return AgentSettings(_env_file=resolved)  # type: ignore[call-arg]


@lru_cache
def get_agent_settings() -> AgentSettings:
    configured_path = os.environ.get("AGENT_CONFIG_FILE")
    return load_agent_settings(Path(configured_path) if configured_path else None)


def main() -> None:
    parser = argparse.ArgumentParser(prog="desktop-collection-agent-check-config")
    parser.add_argument("--config", required=True, type=Path)
    options = parser.parse_args()
    try:
        load_agent_settings(options.config)
    except (OSError, ValueError) as error:
        print("invalid Agent configuration", file=sys.stderr)
        raise SystemExit(1) from error
    print("Agent configuration is valid")

import pytest
from crawler_api.config import Settings
from pydantic import ValidationError


def test_database_defaults_match_version_04_baseline() -> None:
    settings = Settings(
        postgres_host="postgres",
        postgres_database="crawler",
        postgres_username="crawler",
        postgres_password="database-password-value",
        api_token_hash_key="api-hash-value-that-is-at-least-32-bytes",
        agent_device_credential_hash_key="device-hash-value-that-is-at-least-32-bytes",
        agent_token_hash_key="agent-hash-value-that-is-at-least-32-bytes",
        callback_token_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        callback_token_fingerprint_key="callback-fingerprint-value-that-is-at-least-32-bytes",
    )

    assert settings.database_pool_size == 10
    assert settings.database_max_overflow == 0
    assert settings.app_timezone == "Asia/Shanghai"
    assert not settings.database_url.query
    assert settings.database_connect_args["ssl"] == "prefer"
    assert settings.database_connect_args["timeout"] == 10
    assert settings.database_connect_args["server_settings"] == {"timezone": "Asia/Shanghai"}
    assert settings.webhook_dispatcher_enabled
    assert settings.webhook_dispatcher_concurrency == 4
    assert settings.webhook_request_timeout_seconds == 10
    assert settings.webhook_claim_lease_seconds == 30
    assert settings.webhook_max_attempts == 8
    assert settings.webhook_retry_base_seconds == 5
    assert settings.webhook_retry_max_seconds == 900
    assert settings.webhook_metrics_enabled
    serialized = repr(settings)
    assert "database-password-value" not in serialized
    assert "api-hash-value-that-is-at-least-32-bytes" not in serialized
    assert "device-hash-value-that-is-at-least-32-bytes" not in serialized
    assert "agent-hash-value-that-is-at-least-32-bytes" not in serialized
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in serialized
    assert "callback-fingerprint-value-that-is-at-least-32-bytes" not in serialized


def test_security_keys_must_be_at_least_32_characters() -> None:
    with pytest.raises(ValidationError):
        Settings(
            postgres_host="postgres",
            postgres_database="crawler",
            postgres_username="crawler",
            postgres_password="database-password-value",
            api_token_hash_key="short",
            agent_device_credential_hash_key="device-hash-value-that-is-at-least-32-bytes",
            agent_token_hash_key="agent-hash-value-that-is-at-least-32-bytes",
            callback_token_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            callback_token_fingerprint_key=("callback-fingerprint-value-that-is-at-least-32-bytes"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("callback_token_encryption_key", "short"),
        ("callback_token_encryption_key", "A" * 44),
        ("callback_token_fingerprint_key", "short"),
        ("callback_token_encryption_key_version", "invalid version"),
    ],
)
def test_callback_token_key_settings_are_validated(field: str, value: str) -> None:
    values = {
        "postgres_host": "postgres",
        "postgres_database": "crawler",
        "postgres_username": "crawler",
        "postgres_password": "database-password-value",
        "api_token_hash_key": "api-hash-value-that-is-at-least-32-bytes",
        "agent_device_credential_hash_key": ("device-hash-value-that-is-at-least-32-bytes"),
        "agent_token_hash_key": "agent-hash-value-that-is-at-least-32-bytes",
        "callback_token_encryption_key": ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        "callback_token_fingerprint_key": ("callback-fingerprint-value-that-is-at-least-32-bytes"),
        field: value,
    }

    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"webhook_request_timeout_seconds": 30, "webhook_claim_lease_seconds": 30},
        {"webhook_retry_base_seconds": 301, "webhook_retry_max_seconds": 300},
    ],
)
def test_webhook_dispatcher_timing_settings_are_consistent(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "postgres_host": "postgres",
        "postgres_database": "crawler",
        "postgres_username": "crawler",
        "postgres_password": "database-password-value",
        "api_token_hash_key": "api-hash-value-that-is-at-least-32-bytes",
        "agent_device_credential_hash_key": ("device-hash-value-that-is-at-least-32-bytes"),
        "agent_token_hash_key": "agent-hash-value-that-is-at-least-32-bytes",
        "callback_token_encryption_key": ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        "callback_token_fingerprint_key": ("callback-fingerprint-value-that-is-at-least-32-bytes"),
        **overrides,
    }
    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]

from uuid import UUID

import pytest
from crawler_api.infrastructure.security.callback_tokens import (
    CallbackTokenProtector,
    ProtectedCallbackToken,
)
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr

ENCRYPTION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FINGERPRINT_KEY = "callback-fingerprint-key-that-is-at-least-32-bytes"
CALLBACK_CONFIG_ID = UUID("11111111-1111-4111-8111-111111111111")
COLLECTION_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
TOKEN = SecretStr("callback_token_12345678901234567890123456789")


def protector(*, key_version: str = "v1") -> CallbackTokenProtector:
    return CallbackTokenProtector(
        encryption_key=SecretStr(ENCRYPTION_KEY),
        fingerprint_key=SecretStr(FINGERPRINT_KEY),
        encryption_key_version=key_version,
    )


def protect() -> ProtectedCallbackToken:
    return protector().protect(
        TOKEN,
        callback_config_id=CALLBACK_CONFIG_ID,
        collection_job_id=COLLECTION_JOB_ID,
        credential_version=1,
    )


def test_protection_uses_random_nonce_and_stable_fingerprint() -> None:
    first = protect()
    second = protect()

    assert len(first.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint.startswith("hmac-sha256:")
    assert TOKEN.get_secret_value().encode() not in first.ciphertext
    assert TOKEN.get_secret_value() not in repr(first)


def test_protected_token_round_trips_with_exact_aad() -> None:
    protected = protect()

    decrypted = protector().decrypt(
        protected,
        callback_config_id=CALLBACK_CONFIG_ID,
        collection_job_id=COLLECTION_JOB_ID,
        credential_version=1,
    )

    assert decrypted.get_secret_value() == TOKEN.get_secret_value()


@pytest.mark.parametrize(
    ("callback_config_id", "collection_job_id", "credential_version"),
    [
        (UUID("33333333-3333-4333-8333-333333333333"), COLLECTION_JOB_ID, 1),
        (CALLBACK_CONFIG_ID, UUID("44444444-4444-4444-8444-444444444444"), 1),
        (CALLBACK_CONFIG_ID, COLLECTION_JOB_ID, 2),
    ],
)
def test_decryption_rejects_changed_aad(
    callback_config_id: UUID,
    collection_job_id: UUID,
    credential_version: int,
) -> None:
    with pytest.raises(InvalidTag):
        protector().decrypt(
            protect(),
            callback_config_id=callback_config_id,
            collection_job_id=collection_job_id,
            credential_version=credential_version,
        )


def test_decryption_rejects_unavailable_key_version() -> None:
    with pytest.raises(ValueError, match="version is unavailable"):
        protector(key_version="v2").decrypt(
            protect(),
            callback_config_id=CALLBACK_CONFIG_ID,
            collection_job_id=COLLECTION_JOB_ID,
            credential_version=1,
        )


@pytest.mark.parametrize(
    "key",
    [
        "too-short",
        "!" * 43,
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB",
    ],
)
def test_encryption_key_must_be_canonical_base64url_for_32_bytes(key: str) -> None:
    with pytest.raises(ValueError, match="encryption key"):
        CallbackTokenProtector(
            encryption_key=SecretStr(key),
            fingerprint_key=SecretStr(FINGERPRINT_KEY),
            encryption_key_version="v1",
        )

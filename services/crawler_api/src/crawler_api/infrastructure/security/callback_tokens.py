import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

CALLBACK_CONFIG_SCHEMA = "crawler.collection_job_callback_configs"
CALLBACK_TOKEN_NONCE_BYTES = 12


@dataclass(frozen=True, slots=True)
class ProtectedCallbackToken:
    ciphertext: bytes
    nonce: bytes
    fingerprint: str
    encryption_key_version: str


class CallbackTokenProtector:
    def __init__(
        self,
        *,
        encryption_key: SecretStr,
        fingerprint_key: SecretStr,
        encryption_key_version: str,
    ) -> None:
        self._encryption_key = _decode_aes256_key(encryption_key)
        self._fingerprint_key = fingerprint_key.get_secret_value().encode("utf-8")
        if len(self._fingerprint_key) < 32:
            raise ValueError("callback Token fingerprint key must be at least 32 bytes")
        if not encryption_key_version or len(encryption_key_version) > 40:
            raise ValueError("callback Token encryption key version is invalid")
        self._encryption_key_version = encryption_key_version

    def protect(
        self,
        plaintext_token: SecretStr,
        *,
        callback_config_id: UUID,
        collection_job_id: UUID,
        credential_version: int,
    ) -> ProtectedCallbackToken:
        token = plaintext_token.get_secret_value()
        nonce = secrets.token_bytes(CALLBACK_TOKEN_NONCE_BYTES)
        ciphertext = AESGCM(self._encryption_key).encrypt(
            nonce,
            token.encode("utf-8"),
            callback_token_aad(
                callback_config_id=callback_config_id,
                collection_job_id=collection_job_id,
                credential_version=credential_version,
            ),
        )
        return ProtectedCallbackToken(
            ciphertext=ciphertext,
            nonce=nonce,
            fingerprint=self.fingerprint(plaintext_token),
            encryption_key_version=self._encryption_key_version,
        )

    def decrypt(
        self,
        protected: ProtectedCallbackToken,
        *,
        callback_config_id: UUID,
        collection_job_id: UUID,
        credential_version: int,
    ) -> SecretStr:
        if protected.encryption_key_version != self._encryption_key_version:
            raise ValueError("callback Token encryption key version is unavailable")
        plaintext = AESGCM(self._encryption_key).decrypt(
            protected.nonce,
            protected.ciphertext,
            callback_token_aad(
                callback_config_id=callback_config_id,
                collection_job_id=collection_job_id,
                credential_version=credential_version,
            ),
        )
        return SecretStr(plaintext.decode("utf-8"))

    def fingerprint(self, plaintext_token: SecretStr) -> str:
        digest = hmac.new(
            self._fingerprint_key,
            plaintext_token.get_secret_value().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"


def callback_token_aad(
    *,
    callback_config_id: UUID,
    collection_job_id: UUID,
    credential_version: int,
) -> bytes:
    return json.dumps(
        {
            "callbackConfigId": str(callback_config_id),
            "collectionJobId": str(collection_job_id),
            "credentialVersion": credential_version,
            "schema": CALLBACK_CONFIG_SCHEMA,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _decode_aes256_key(value: SecretStr) -> bytes:
    try:
        encoded = value.get_secret_value()
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError(
            "callback Token encryption key must be unpadded base64url"
        ) from error
    if len(decoded) != 32:
        raise ValueError("callback Token encryption key must decode to 32 bytes")
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if not hmac.compare_digest(encoded, canonical):
        raise ValueError("callback Token encryption key must use canonical base64url")
    return decoded

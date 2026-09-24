import hashlib
import hmac

from pydantic import SecretStr


class DeviceCredentialHasher:
    """Hash long-lived Agent device credentials with their dedicated key domain."""

    def __init__(self, hash_key: SecretStr) -> None:
        self._hash_key = hash_key.get_secret_value().encode()

    def hash(self, device_secret: str) -> str:
        digest = hmac.new(
            self._hash_key,
            device_secret.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def verify(self, device_secret: str, stored_hash: str) -> bool:
        return hmac.compare_digest(self.hash(device_secret), stored_hash)

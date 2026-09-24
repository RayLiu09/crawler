import hashlib
import hmac

from pydantic import SecretStr


class AccessTokenHasher:
    def __init__(self, hash_key: SecretStr) -> None:
        self._hash_key = hash_key.get_secret_value().encode()

    def hash(self, plaintext_token: str) -> str:
        digest = hmac.new(
            self._hash_key,
            plaintext_token.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

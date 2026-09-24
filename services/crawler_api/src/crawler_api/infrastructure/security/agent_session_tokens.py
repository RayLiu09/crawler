import hashlib
import hmac

from pydantic import SecretStr


class AgentSessionTokenHasher:
    """Hash short-lived Agent Session Tokens in an independent key domain."""

    def __init__(self, hash_key: SecretStr) -> None:
        self._hash_key = hash_key.get_secret_value().encode()

    def hash(self, session_token: str) -> str:
        digest = hmac.new(
            self._hash_key,
            session_token.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

import base64
import hashlib
import hmac
from uuid import UUID

from pydantic import SecretStr


class TaskLeaseTokenManager:
    """Issue replayable lease Tokens from domain-separated Agent key material."""

    def __init__(self, agent_token_hash_key: SecretStr) -> None:
        master_key = agent_token_hash_key.get_secret_value().encode()
        self._issuance_key = hmac.new(
            master_key,
            b"crawler-task-lease-token-issuance-v1",
            hashlib.sha256,
        ).digest()
        self._storage_key = hmac.new(
            master_key,
            b"crawler-task-lease-token-storage-v1",
            hashlib.sha256,
        ).digest()

    def issue(
        self,
        *,
        attempt_id: UUID,
        agent_id: UUID,
        task_id: UUID,
        fencing_token: int,
    ) -> str:
        context = f"{attempt_id}:{agent_id}:{task_id}:{fencing_token}".encode("ascii")
        digest = hmac.new(self._issuance_key, context, hashlib.sha256).digest()
        encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"lse_{encoded}"

    def hash(self, lease_token: str) -> str:
        digest = hmac.new(
            self._storage_key,
            lease_token.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def matches(self, lease_token: str, stored_hash: str) -> bool:
        return hmac.compare_digest(self.hash(lease_token), stored_hash)

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class CollectionJobCallbackConfig:
    collection_job_id: UUID
    target_url: str
    token_ciphertext: bytes | None
    token_nonce: bytes | None
    token_fingerprint: str
    token_expires_at: datetime
    encryption_key_version: str
    id: UUID = field(default_factory=uuid4)
    auth_scheme: str = "bearer"
    credential_version: int = 1
    credential_destroyed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CollectionJobCallbackCredentialRotation:
    callback_config_id: UUID
    idempotency_key_hash: str
    request_fingerprint: str
    result_credential_version: int
    token_expires_at: datetime
    id: UUID = field(default_factory=uuid4)
    created_at: datetime | None = None

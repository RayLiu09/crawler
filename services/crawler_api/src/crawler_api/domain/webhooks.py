from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from crawler_api.domain.callbacks import CollectionJobCallbackConfig
from crawler_api.domain.events import StoredCollectionEvent


class WebhookOutboxStatus(StrEnum):
    PENDING = "pending"
    CREDENTIAL_BLOCKED = "credential_blocked"
    DELIVERED = "delivered"
    PERMANENTLY_FAILED = "permanently_failed"


@dataclass(frozen=True, slots=True)
class WebhookOutboxEntry:
    event_id: UUID
    callback_config_id: UUID
    next_attempt_at: datetime
    id: UUID = field(default_factory=uuid4)
    status: WebhookOutboxStatus = WebhookOutboxStatus.PENDING
    attempt_count: int = 0
    delivered_at: datetime | None = None
    last_failure_code: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredWebhookDelivery:
    id: UUID
    event_id: UUID
    event_type: str
    status: WebhookOutboxStatus
    attempt_count: int
    replay_count: int
    next_attempt_at: datetime
    delivered_at: datetime | None
    last_failure_code: str | None
    dispatch_claim_id: UUID | None
    dispatch_lease_expires_at: datetime | None
    last_replayed_at: datetime | None
    created_at: datetime
    credential_available: bool
    token_expires_at: datetime


@dataclass(frozen=True, slots=True)
class WebhookDeliveryReplay:
    webhook_outbox_id: UUID
    idempotency_key_hash: str
    replay_number: int
    previous_attempt_count: int
    previous_failure_code: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClaimedWebhookDelivery:
    outbox_id: UUID
    claim_id: UUID
    event: StoredCollectionEvent
    callback_config: CollectionJobCallbackConfig
    attempt_number: int
    claimed_at: datetime
    lease_expires_at: datetime
    recovered_expired_claim: bool = False


@dataclass(frozen=True, slots=True)
class WebhookDeliverySettlement:
    outbox_id: UUID
    claim_id: UUID
    claimed_credential_version: int
    status: WebhookOutboxStatus
    settled_at: datetime
    next_attempt_at: datetime | None = None
    failure_code: str | None = None
    attempted: bool = True


@dataclass(frozen=True, slots=True)
class WebhookQueueMetricsSnapshot:
    pending_count: int
    oldest_pending_created_at: datetime | None
    credential_blocked_count: int
    permanently_failed_count: int
    retry_exhausted_count: int
    active_claim_count: int

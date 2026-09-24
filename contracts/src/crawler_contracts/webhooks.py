from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .common import ApiModel
from .events import CollectionEventType

WebhookDeliveryStatus = Literal[
    "pending",
    "credential_blocked",
    "delivered",
    "permanently_failed",
]


class WebhookDelivery(ApiModel):
    outbox_id: UUID
    event_id: UUID
    event_type: CollectionEventType
    status: WebhookDeliveryStatus
    attempt_count: int = Field(ge=0)
    replay_count: int = Field(ge=0)
    replay_eligible: bool
    next_attempt_at: datetime
    delivered_at: datetime | None
    last_failure_code: str | None
    active_claim: bool
    claim_expires_at: datetime | None
    last_replayed_at: datetime | None
    created_at: datetime


class WebhookDeliveryPageResponse(ApiModel):
    items: list[WebhookDelivery]
    next_cursor: str | None = None


class ReplayWebhookDeliveryResponse(ApiModel):
    outbox_id: UUID
    status: Literal["pending"] = "pending"
    replay_number: int = Field(ge=1)
    replayed_at: datetime
    previous_attempt_count: int = Field(ge=1)
    previous_failure_code: str


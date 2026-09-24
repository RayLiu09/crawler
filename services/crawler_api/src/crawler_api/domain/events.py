from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from crawler_contracts.events import CollectionEventPayload, CollectionEventType


@dataclass(frozen=True, slots=True)
class StoredCollectionEvent:
    collection_job_id: UUID
    external_request_id: str
    event_type: CollectionEventType
    job_version: int
    payload: CollectionEventPayload
    occurred_at: datetime
    id: UUID = field(default_factory=uuid4)
    schema_version: int = 1

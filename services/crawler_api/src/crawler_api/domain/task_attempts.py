from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4


class TaskAttemptOutcome(StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
    LEASE_EXPIRED = "lease_expired"


@dataclass(frozen=True, slots=True)
class CollectionTaskAttempt:
    collection_task_id: UUID
    agent_id: UUID
    fencing_token: int
    claimed_at: datetime
    id: UUID = field(default_factory=uuid4)
    released_at: datetime | None = None
    outcome: TaskAttemptOutcome | None = None
    failure_code: str | None = None

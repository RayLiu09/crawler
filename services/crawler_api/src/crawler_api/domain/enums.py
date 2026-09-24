from enum import StrEnum


class DesiredState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class CollectionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    AWAITING_HUMAN = "awaiting_human"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"


TERMINAL_COLLECTION_STATES = frozenset(
    {
        CollectionState.CANCELLED,
        CollectionState.SUCCEEDED,
        CollectionState.PARTIALLY_SUCCEEDED,
        CollectionState.FAILED,
    }
)

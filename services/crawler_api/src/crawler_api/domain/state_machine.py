from .enums import CollectionState

ALLOWED_TRANSITIONS: dict[CollectionState, frozenset[CollectionState]] = {
    CollectionState.QUEUED: frozenset(
        {CollectionState.RUNNING, CollectionState.PAUSED, CollectionState.CANCELLED}
    ),
    CollectionState.RUNNING: frozenset(
        {
            CollectionState.PAUSING,
            CollectionState.AWAITING_HUMAN,
            CollectionState.CANCELLING,
            CollectionState.SUCCEEDED,
            CollectionState.PARTIALLY_SUCCEEDED,
            CollectionState.FAILED,
        }
    ),
    CollectionState.PAUSING: frozenset({CollectionState.PAUSED, CollectionState.CANCELLING}),
    CollectionState.PAUSED: frozenset({CollectionState.QUEUED, CollectionState.CANCELLED}),
    CollectionState.AWAITING_HUMAN: frozenset(
        {
            CollectionState.RUNNING,
            CollectionState.PAUSING,
            CollectionState.CANCELLING,
            CollectionState.FAILED,
        }
    ),
    CollectionState.CANCELLING: frozenset({CollectionState.CANCELLED}),
    CollectionState.CANCELLED: frozenset(),
    CollectionState.SUCCEEDED: frozenset(),
    CollectionState.PARTIALLY_SUCCEEDED: frozenset(),
    CollectionState.FAILED: frozenset(),
}


def can_transition(current: CollectionState, target: CollectionState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]

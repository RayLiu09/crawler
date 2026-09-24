from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .collection_jobs import CollectionStatusValue, DesiredStateValue
from .common import ApiModel, SourceCode


class CollectionEventType(StrEnum):
    JOB_ACCEPTED = "collection.job.accepted"
    JOB_DESIRED_STATE_CHANGED = "collection.job.desired_state_changed"
    JOB_TERMINAL = "collection.job.terminal"


class CollectionJobAcceptedEventPayload(ApiModel):
    source: SourceCode
    desired_state: Literal["running"] = "running"
    status: Literal["queued"] = "queued"
    task_count: int = Field(ge=1)


class CollectionJobDesiredStateChangedEventPayload(ApiModel):
    action: Literal["pause", "resume", "cancel"]
    desired_state: DesiredStateValue
    status: CollectionStatusValue


class CollectionJobTerminalEventPayload(ApiModel):
    status: Literal["cancelled", "succeeded", "partially_succeeded", "failed"]
    processed_count: int = Field(ge=0)
    failure_code_counts: dict[str, int]
    completed_at: datetime

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> Self:
        if self.completed_at.tzinfo is None:
            raise ValueError("completedAt must include a timezone")
        if any(not code or count < 1 for code, count in self.failure_code_counts.items()):
            raise ValueError("failureCodeCounts must contain non-empty codes and positive counts")
        return self


CollectionEventPayload = (
    CollectionJobAcceptedEventPayload
    | CollectionJobDesiredStateChangedEventPayload
    | CollectionJobTerminalEventPayload
)


class CollectionEvent(ApiModel):
    schema_version: Literal[1] = 1
    event_id: UUID
    event_type: CollectionEventType
    occurred_at: datetime
    collection_job_id: UUID
    external_request_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    job_version: int = Field(ge=1)
    payload: CollectionEventPayload

    @model_validator(mode="after")
    def validate_event_envelope(self) -> Self:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurredAt must include a timezone")
        expected_payload_types = {
            CollectionEventType.JOB_ACCEPTED: CollectionJobAcceptedEventPayload,
            CollectionEventType.JOB_DESIRED_STATE_CHANGED: (
                CollectionJobDesiredStateChangedEventPayload
            ),
            CollectionEventType.JOB_TERMINAL: CollectionJobTerminalEventPayload,
        }
        if not isinstance(self.payload, expected_payload_types[self.event_type]):
            raise ValueError("payload does not match eventType")
        return self


class CollectionEventPageResponse(ApiModel):
    items: list[CollectionEvent]
    next_cursor: str | None = None

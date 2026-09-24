from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, HttpUrl, SecretStr, field_validator

from .common import ApiModel, SourceCode

JsonScalar = str | int | float | bool | None


class CollectionQuery(ApiModel):
    keyword: str = Field(min_length=1, max_length=120)
    region: str = Field(default="", max_length=80)
    page_limit: int = Field(default=10, ge=1, le=30)


class CollectionJobPayload(ApiModel):
    """Canonical snapshot stored in collection_jobs.request_payload."""

    schema_version: Literal[1] = 1
    source: SourceCode
    queries: list[CollectionQuery] = Field(min_length=1, max_length=100)
    collection_mode: Literal["full", "incremental"] = "full"
    metadata: dict[str, JsonScalar] = Field(default_factory=dict, max_length=50)


class CollectionJobCallback(ApiModel):
    url: HttpUrl
    access_token: SecretStr = Field(min_length=43, max_length=1024)
    token_type: Literal["Bearer"]
    expires_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def validate_callback_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.fragment:
            raise ValueError("callback url cannot contain userinfo or a fragment")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiry_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("callback expiresAt must include a timezone")
        return value


class SubmitCollectionJobRequest(CollectionJobPayload):
    external_request_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    callback: CollectionJobCallback | None = None

    def canonical_payload(self) -> CollectionJobPayload:
        """Return the immutable, caller-context-free JSONB payload snapshot."""
        return CollectionJobPayload.model_validate(
            self.model_dump(exclude={"external_request_id", "callback"})
        )


class SubmitCollectionJobResponse(ApiModel):
    collection_job_id: UUID
    external_request_id: str
    status: Literal["queued"] = "queued"
    callback_token_expires_at: datetime | None = None


class RotateCollectionJobCallbackTokenRequest(ApiModel):
    access_token: SecretStr = Field(min_length=43, max_length=1024)
    token_type: Literal["Bearer"]
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("callback expiresAt must include a timezone")
        return value


class RotateCollectionJobCallbackTokenResponse(ApiModel):
    callback_token_expires_at: datetime
    credential_version: int = Field(ge=2)


DesiredStateValue = Literal["running", "paused", "cancelled"]
CollectionStatusValue = Literal[
    "queued",
    "running",
    "pausing",
    "paused",
    "awaiting_human",
    "cancelling",
    "cancelled",
    "succeeded",
    "partially_succeeded",
    "failed",
]


class CollectionTaskStatusCounts(ApiModel):
    total: int = Field(ge=0)
    queued: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    pausing: int = Field(default=0, ge=0)
    paused: int = Field(default=0, ge=0)
    awaiting_human: int = Field(default=0, ge=0)
    cancelling: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    partially_succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class CollectionJobResponse(ApiModel):
    collection_job_id: UUID
    external_request_id: str
    source: SourceCode
    desired_state: DesiredStateValue
    status: CollectionStatusValue
    version: int = Field(ge=1)
    task_status_counts: CollectionTaskStatusCounts
    processed_count: int = Field(ge=0)
    failure_code_counts: dict[str, int]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

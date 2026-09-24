from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from .common import ApiModel, SourceCode
from .parsed_jobs import ParsedJobRecord


class AgentSessionRequest(ApiModel):
    agent_id: UUID
    device_secret: SecretStr


class AgentSessionResponse(ApiModel):
    session_token: str = Field(min_length=32)
    expires_at: datetime


class BrowserProfileStatus(ApiModel):
    source: SourceCode
    profile_key: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    status: Literal["ready", "busy", "login_required", "unavailable"]


class BrowserCapability(ApiModel):
    name: Literal["chrome"] = "chrome"
    headful: Literal[True] = True


class AgentCapabilities(ApiModel):
    schema_version: Literal[1] = 1
    supported_sources: list[SourceCode] = Field(default_factory=list)
    max_concurrent_tasks: int = Field(default=1, ge=1, le=32)
    browser: BrowserCapability = Field(default_factory=BrowserCapability)


class AgentHeartbeatRequest(ApiModel):
    agent_id: UUID
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    available_slots: int = Field(ge=0, le=32)
    profiles: list[BrowserProfileStatus] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_runtime_capacity(self) -> Self:
        if self.available_slots > self.capabilities.max_concurrent_tasks:
            raise ValueError("availableSlots cannot exceed capabilities.maxConcurrentTasks")
        unsupported_profiles = {
            profile.source
            for profile in self.profiles
            if profile.source not in self.capabilities.supported_sources
        }
        if unsupported_profiles:
            raise ValueError("profile source must be present in capabilities.supportedSources")
        profile_keys = [(profile.source, profile.profile_key) for profile in self.profiles]
        if len(profile_keys) != len(set(profile_keys)):
            raise ValueError("profiles cannot contain duplicate source/profileKey pairs")
        return self


class ClaimTaskRequest(ApiModel):
    agent_id: UUID
    claim_request_id: UUID


class TaskLease(ApiModel):
    agent_id: UUID
    task_id: UUID
    attempt_id: UUID
    source: SourceCode
    profile_key: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    keyword: str = Field(min_length=1, max_length=120)
    region: str = Field(default="", max_length=80)
    page_limit: int = Field(ge=1, le=30)
    page_checkpoint: int = Field(ge=0)
    lease_token: str = Field(min_length=32)
    fencing_token: int = Field(ge=1)
    lease_expires_at: datetime


class LeaseReference(ApiModel):
    agent_id: UUID
    task_id: UUID
    lease_token: SecretStr
    fencing_token: int = Field(ge=1)
    lease_expires_at: datetime


class TaskCheckpointRequest(LeaseReference):
    page_checkpoint: int = Field(ge=0)
    processed_count: int = Field(ge=0)


class ParsedRecordBatchRequest(LeaseReference):
    batch_id: UUID
    records: list[ParsedJobRecord] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_unique_records(self) -> Self:
        identities = [
            (str(record.source_url), record.payload_hash) for record in self.records
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("records cannot contain duplicate sourceUrl/payloadHash pairs")
        return self


class CompleteTaskRequest(LeaseReference):
    outcome: Literal["completed", "paused", "cancelled", "failed"]
    failure_code: str | None = Field(default=None, max_length=100)


class ReleaseTaskRequest(LeaseReference):
    failure_code: str = Field(default="agent_release", min_length=1, max_length=100)


class TaskControlResponse(ApiModel):
    desired_state: Literal["running", "paused", "cancelled"]
    lease_expires_at: datetime

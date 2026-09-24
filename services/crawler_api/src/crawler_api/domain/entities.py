from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from crawler_contracts.agent_protocol import AgentCapabilities
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import ApiScope, SourceCode

from .enums import CollectionState, DesiredState


@dataclass(slots=True)
class ApiClient:
    tenant_id: str
    display_name: str
    allowed_scopes: list[ApiScope]
    config_fingerprint: str
    status: str = "active"
    id: UUID = field(default_factory=uuid4)
    last_token_issued_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ApiAccessToken:
    client_id: UUID
    token_hash: str
    issued_via_key_fingerprint: str
    scopes: list[ApiScope]
    issued_at: datetime
    expires_at: datetime
    id: UUID = field(default_factory=uuid4)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(slots=True)
class CollectionAgent:
    device_credential_hash: str
    display_name: str
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    status: str = "offline"
    id: UUID = field(default_factory=uuid4)
    current_task_id: UUID | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class AgentSession:
    agent_id: UUID
    session_token_hash: str
    issued_at: datetime
    expires_at: datetime
    id: UUID = field(default_factory=uuid4)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(slots=True)
class BrowserProfile:
    agent_id: UUID
    source: SourceCode
    profile_key: str
    status: str
    id: UUID = field(default_factory=uuid4)
    updated_at: datetime | None = None


@dataclass(slots=True)
class CollectionJob:
    client_id: UUID
    external_request_id: str
    idempotency_key_hash: str
    request_payload: CollectionJobPayload
    desired_state: DesiredState = DesiredState.RUNNING
    status: CollectionState = CollectionState.QUEUED
    id: UUID = field(default_factory=uuid4)
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class CollectionTask:
    collection_job_id: UUID
    source: SourceCode
    keyword: str
    region: str
    page_limit: int
    desired_state: DesiredState = DesiredState.RUNNING
    status: CollectionState = CollectionState.QUEUED
    id: UUID = field(default_factory=uuid4)
    page_checkpoint: int = 0
    leased_agent_id: UUID | None = None
    leased_profile_id: UUID | None = None
    lease_token_hash: str | None = None
    fencing_token: int = 0
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    processed_count: int = 0
    failure_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from crawler_api.domain.callbacks import (
    CollectionJobCallbackConfig,
    CollectionJobCallbackCredentialRotation,
)
from crawler_api.domain.entities import (
    AgentSession,
    ApiAccessToken,
    ApiClient,
    BrowserProfile,
    CollectionAgent,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.events import StoredCollectionEvent
from crawler_api.domain.parsed_records import (
    ParsedRecordBatchReceipt,
    StoredParsedJobRecord,
)
from crawler_api.domain.task_attempts import CollectionTaskAttempt
from crawler_api.domain.webhooks import (
    ClaimedWebhookDelivery,
    StoredWebhookDelivery,
    WebhookDeliveryReplay,
    WebhookDeliverySettlement,
    WebhookOutboxEntry,
    WebhookOutboxStatus,
    WebhookQueueMetricsSnapshot,
)
from crawler_contracts.common import SourceCode


class ApiClientRepository(Protocol):
    async def add(self, client: ApiClient) -> None: ...

    async def get_by_tenant_id(self, tenant_id: str) -> ApiClient | None: ...

    async def get_for_update(self, client_id: UUID) -> ApiClient | None: ...

    async def get_for_share(self, client_id: UUID) -> ApiClient | None: ...

    async def list_all(self) -> list[ApiClient]: ...

    async def update(self, client: ApiClient) -> None: ...


class ApiAccessTokenRepository(Protocol):
    async def add(self, token: ApiAccessToken) -> None: ...

    async def get(self, token_id: UUID) -> ApiAccessToken | None: ...

    async def get_by_token_hash(self, token_hash: str) -> ApiAccessToken | None: ...

    async def update_last_used(self, token_id: UUID, last_used_at: datetime) -> None: ...

    async def revoke_active_for_clients(self, client_ids: set[UUID]) -> int: ...


class CollectionAgentRepository(Protocol):
    async def add(self, agent: CollectionAgent) -> None: ...

    async def get(self, agent_id: UUID) -> CollectionAgent | None: ...

    async def get_for_update(self, agent_id: UUID) -> CollectionAgent | None: ...

    async def update(self, agent: CollectionAgent) -> None: ...

    async def clear_current_task(
        self, agent_id: UUID, task_id: UUID, updated_at: datetime
    ) -> bool: ...


class AgentSessionRepository(Protocol):
    async def add(self, session: AgentSession) -> None: ...

    async def get(self, session_id: UUID) -> AgentSession | None: ...

    async def get_by_token_hash(self, token_hash: str) -> AgentSession | None: ...

    async def update_last_used(self, session_id: UUID, last_used_at: datetime) -> None: ...

    async def revoke_active_for_agent(self, agent_id: UUID, revoked_at: datetime) -> int: ...


class BrowserProfileRepository(Protocol):
    async def get(self, profile_id: UUID) -> BrowserProfile | None: ...

    async def list_for_agent(self, agent_id: UUID) -> list[BrowserProfile]: ...

    async def upsert(self, profile: BrowserProfile) -> None: ...


class CollectionJobRepository(Protocol):
    async def add(self, job: CollectionJob) -> None: ...

    async def try_add(self, job: CollectionJob) -> bool: ...

    async def get_for_client(self, job_id: UUID, client_id: UUID) -> CollectionJob | None: ...

    async def get_for_client_for_update(
        self, job_id: UUID, client_id: UUID
    ) -> CollectionJob | None: ...

    async def get_for_update(self, job_id: UUID) -> CollectionJob | None: ...

    async def lock_next_claimable(self, sources: list[SourceCode]) -> CollectionJob | None: ...

    async def find_next_with_expired_lease(self, expired_at: datetime) -> CollectionJob | None: ...

    async def get_by_external_request_id(
        self, client_id: UUID, external_request_id: str
    ) -> CollectionJob | None: ...

    async def get_by_idempotency_hash(
        self, client_id: UUID, idempotency_key_hash: str
    ) -> CollectionJob | None: ...

    async def update_control_state(self, job: CollectionJob, updated_at: datetime) -> None: ...

    async def update_runtime_state(self, job: CollectionJob, updated_at: datetime) -> None: ...


class CollectionTaskRepository(Protocol):
    async def add(self, task: CollectionTask) -> None: ...

    async def get(self, task_id: UUID) -> CollectionTask | None: ...

    async def get_for_update(self, task_id: UUID) -> CollectionTask | None: ...

    async def lock_next_claimable(
        self, job_id: UUID, sources: list[SourceCode]
    ) -> CollectionTask | None: ...

    async def find_expired_for_job(
        self, job_id: UUID, expired_at: datetime
    ) -> CollectionTask | None: ...

    async def list_for_job(self, job_id: UUID) -> list[CollectionTask]: ...

    async def list_for_job_for_update(self, job_id: UUID) -> list[CollectionTask]: ...

    async def update_control_state(self, task: CollectionTask, updated_at: datetime) -> None: ...

    async def update_lease_state(self, task: CollectionTask, updated_at: datetime) -> None: ...

    async def update_runtime_state(self, task: CollectionTask, updated_at: datetime) -> None: ...


class CollectionTaskAttemptRepository(Protocol):
    async def add(self, attempt: CollectionTaskAttempt) -> None: ...

    async def get(self, attempt_id: UUID) -> CollectionTaskAttempt | None: ...

    async def get_by_task_fencing(
        self, task_id: UUID, fencing_token: int
    ) -> CollectionTaskAttempt | None: ...

    async def update_outcome(self, attempt: CollectionTaskAttempt) -> None: ...


class ParsedJobRecordRepository(Protocol):
    async def insert_many(
        self, records: list[StoredParsedJobRecord], created_at: datetime
    ) -> tuple[list[StoredParsedJobRecord], int]: ...

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        after_created_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredParsedJobRecord]: ...


class ParsedRecordBatchRepository(Protocol):
    async def get(self, batch_id: UUID) -> ParsedRecordBatchReceipt | None: ...

    async def try_add(self, receipt: ParsedRecordBatchReceipt) -> bool: ...


class CollectionEventRepository(Protocol):
    async def add(self, event: StoredCollectionEvent) -> None: ...

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        after_occurred_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredCollectionEvent]: ...


class CollectionJobCallbackConfigRepository(Protocol):
    async def add(self, config: CollectionJobCallbackConfig) -> None: ...

    async def get_for_job(self, job_id: UUID) -> CollectionJobCallbackConfig | None: ...

    async def update_credentials(
        self,
        config: CollectionJobCallbackConfig,
        *,
        expected_credential_version: int,
        updated_at: datetime,
    ) -> bool: ...


class CollectionJobCallbackCredentialRotationRepository(Protocol):
    async def add(self, rotation: CollectionJobCallbackCredentialRotation) -> None: ...

    async def get(
        self, callback_config_id: UUID, idempotency_key_hash: str
    ) -> CollectionJobCallbackCredentialRotation | None: ...


class WebhookOutboxRepository(Protocol):
    async def add(self, entry: WebhookOutboxEntry) -> None: ...

    async def has_undelivered_for_callback(self, callback_config_id: UUID) -> bool: ...

    async def resume_credential_blocked(
        self, callback_config_id: UUID, resumed_at: datetime
    ) -> int: ...

    async def claim_due(
        self,
        *,
        claim_id: UUID,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> ClaimedWebhookDelivery | None: ...

    async def settle_claim(
        self, settlement: WebhookDeliverySettlement
    ) -> WebhookDeliverySettlement | None: ...

    async def get_metrics_snapshot(
        self, observed_at: datetime
    ) -> WebhookQueueMetricsSnapshot: ...

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        status: WebhookOutboxStatus,
        after_created_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredWebhookDelivery]: ...

    async def get_for_client_for_update(
        self,
        *,
        outbox_id: UUID,
        job_id: UUID,
        client_id: UUID,
    ) -> StoredWebhookDelivery | None: ...

    async def activate_replay(
        self,
        outbox_id: UUID,
        *,
        expected_replay_count: int,
        replayed_at: datetime,
    ) -> bool: ...


class WebhookDeliveryReplayRepository(Protocol):
    async def add(self, replay: WebhookDeliveryReplay) -> None: ...

    async def get(
        self, webhook_outbox_id: UUID, idempotency_key_hash: str
    ) -> WebhookDeliveryReplay | None: ...


class LeaseTokenManager(Protocol):
    def issue(
        self,
        *,
        attempt_id: UUID,
        agent_id: UUID,
        task_id: UUID,
        fencing_token: int,
    ) -> str: ...

    def hash(self, lease_token: str) -> str: ...

    def matches(self, lease_token: str, stored_hash: str) -> bool: ...


class UnitOfWork(Protocol):
    @property
    def api_clients(self) -> ApiClientRepository: ...

    @property
    def api_access_tokens(self) -> ApiAccessTokenRepository: ...

    @property
    def agents(self) -> CollectionAgentRepository: ...

    @property
    def agent_sessions(self) -> AgentSessionRepository: ...

    @property
    def browser_profiles(self) -> BrowserProfileRepository: ...

    @property
    def collection_jobs(self) -> CollectionJobRepository: ...

    @property
    def collection_tasks(self) -> CollectionTaskRepository: ...

    @property
    def task_attempts(self) -> CollectionTaskAttemptRepository: ...

    @property
    def parsed_job_records(self) -> ParsedJobRecordRepository: ...

    @property
    def parsed_record_batches(self) -> ParsedRecordBatchRepository: ...

    @property
    def collection_events(self) -> CollectionEventRepository: ...

    @property
    def callback_configs(self) -> CollectionJobCallbackConfigRepository: ...

    @property
    def callback_credential_rotations(
        self,
    ) -> CollectionJobCallbackCredentialRotationRepository: ...

    @property
    def webhook_outbox(self) -> WebhookOutboxRepository: ...

    @property
    def webhook_delivery_replays(self) -> WebhookDeliveryReplayRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from crawler_contracts.agent_protocol import AgentCapabilities
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import ApiScope, SourceCode
from crawler_contracts.events import CollectionEvent
from crawler_contracts.parsed_jobs import ParsedJobFields
from sqlalchemy import and_, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

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
from crawler_api.domain.enums import TERMINAL_COLLECTION_STATES, CollectionState, DesiredState
from crawler_api.domain.events import StoredCollectionEvent
from crawler_api.domain.parsed_records import (
    ParsedRecordBatchReceipt,
    StoredParsedJobRecord,
)
from crawler_api.domain.task_attempts import CollectionTaskAttempt, TaskAttemptOutcome
from crawler_api.domain.webhooks import (
    ClaimedWebhookDelivery,
    StoredWebhookDelivery,
    WebhookDeliveryReplay,
    WebhookDeliverySettlement,
    WebhookOutboxEntry,
    WebhookOutboxStatus,
    WebhookQueueMetricsSnapshot,
)

from .models import (
    AgentSessionRow,
    ApiAccessTokenRow,
    ApiClientRow,
    BrowserProfileRow,
    CollectionAgentRow,
    CollectionEventRow,
    CollectionJobCallbackConfigRow,
    CollectionJobCallbackCredentialRotationRow,
    CollectionJobRow,
    CollectionTaskAttemptRow,
    CollectionTaskRow,
    ParsedJobRecordRow,
    ParsedRecordBatchRow,
    WebhookOutboxReplayRow,
    WebhookOutboxRow,
)


def _to_api_client(row: ApiClientRow) -> ApiClient:
    return ApiClient(
        id=row.id,
        tenant_id=row.tenant_id,
        display_name=row.display_name,
        allowed_scopes=[ApiScope(scope) for scope in row.allowed_scopes],
        config_fingerprint=row.config_fingerprint,
        status=row.status,
        last_token_issued_at=row.last_token_issued_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_api_access_token(row: ApiAccessTokenRow) -> ApiAccessToken:
    return ApiAccessToken(
        id=row.id,
        client_id=row.client_id,
        token_hash=row.token_hash,
        issued_via_key_fingerprint=row.issued_via_key_fingerprint,
        scopes=[ApiScope(scope) for scope in row.scopes],
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _to_stored_webhook_delivery(
    row: WebhookOutboxRow,
    event: CollectionEventRow,
    callback: CollectionJobCallbackConfigRow,
) -> StoredWebhookDelivery:
    return StoredWebhookDelivery(
        id=row.id,
        event_id=row.event_id,
        event_type=event.event_type,
        status=WebhookOutboxStatus(row.status),
        attempt_count=row.attempt_count,
        replay_count=row.replay_count,
        next_attempt_at=row.next_attempt_at,
        delivered_at=row.delivered_at,
        last_failure_code=row.last_failure_code,
        dispatch_claim_id=row.dispatch_claim_id,
        dispatch_lease_expires_at=row.dispatch_lease_expires_at,
        last_replayed_at=row.last_replayed_at,
        created_at=row.created_at,
        credential_available=(
            callback.credential_destroyed_at is None
            and callback.token_ciphertext is not None
            and callback.token_nonce is not None
        ),
        token_expires_at=callback.token_expires_at,
    )


def _to_webhook_delivery_replay(
    row: WebhookOutboxReplayRow,
) -> WebhookDeliveryReplay:
    return WebhookDeliveryReplay(
        id=row.id,
        webhook_outbox_id=row.webhook_outbox_id,
        idempotency_key_hash=row.idempotency_key_hash,
        replay_number=row.replay_number,
        previous_attempt_count=row.previous_attempt_count,
        previous_failure_code=row.previous_failure_code,
        created_at=row.created_at,
    )


def _to_agent(row: CollectionAgentRow) -> CollectionAgent:
    return CollectionAgent(
        id=row.id,
        device_credential_hash=row.device_credential_hash,
        display_name=row.display_name,
        capabilities=AgentCapabilities.model_validate(row.capabilities),
        status=row.status,
        current_task_id=row.current_task_id,
        heartbeat_at=row.heartbeat_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_agent_session(row: AgentSessionRow) -> AgentSession:
    return AgentSession(
        id=row.id,
        agent_id=row.agent_id,
        session_token_hash=row.session_token_hash,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _to_profile(row: BrowserProfileRow) -> BrowserProfile:
    return BrowserProfile(
        id=row.id,
        agent_id=row.agent_id,
        source=SourceCode(row.source),
        profile_key=row.profile_key,
        status=row.status,
        updated_at=row.updated_at,
    )


def _to_job(row: CollectionJobRow) -> CollectionJob:
    return CollectionJob(
        id=row.id,
        client_id=row.client_id,
        external_request_id=row.external_request_id,
        idempotency_key_hash=row.idempotency_key_hash,
        request_payload=CollectionJobPayload.model_validate(row.request_payload),
        desired_state=DesiredState(row.desired_state),
        status=CollectionState(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _to_task(row: CollectionTaskRow) -> CollectionTask:
    return CollectionTask(
        id=row.id,
        collection_job_id=row.collection_job_id,
        source=SourceCode(row.source),
        keyword=row.keyword,
        region=row.region,
        page_limit=row.page_limit,
        page_checkpoint=row.page_checkpoint,
        desired_state=DesiredState(row.desired_state),
        status=CollectionState(row.status),
        leased_agent_id=row.leased_agent_id,
        leased_profile_id=row.leased_profile_id,
        lease_token_hash=row.lease_token_hash,
        fencing_token=row.fencing_token,
        lease_expires_at=row.lease_expires_at,
        attempt_count=row.attempt_count,
        processed_count=row.processed_count,
        failure_code=row.failure_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_task_attempt(row: CollectionTaskAttemptRow) -> CollectionTaskAttempt:
    return CollectionTaskAttempt(
        id=row.id,
        collection_task_id=row.collection_task_id,
        agent_id=row.agent_id,
        fencing_token=row.fencing_token,
        claimed_at=row.claimed_at,
        released_at=row.released_at,
        outcome=TaskAttemptOutcome(row.outcome) if row.outcome is not None else None,
        failure_code=row.failure_code,
    )


def _to_parsed_record(row: ParsedJobRecordRow, source: str) -> StoredParsedJobRecord:
    return StoredParsedJobRecord(
        id=row.id,
        collection_task_id=row.collection_task_id,
        source=SourceCode(source),
        source_job_id=row.source_job_id,
        source_url=row.source_url,
        keyword=str(row.query_context["keyword"]),
        region=str(row.query_context["region"]),
        page=int(row.query_context["page"]),
        fields=ParsedJobFields.model_validate(row.parsed_fields),
        payload_hash=row.payload_hash,
        collected_at=row.collected_at,
        created_at=row.created_at,
    )


def _to_batch_receipt(row: ParsedRecordBatchRow) -> ParsedRecordBatchReceipt:
    return ParsedRecordBatchReceipt(
        id=row.id,
        collection_task_id=row.collection_task_id,
        attempt_id=row.attempt_id,
        request_hash=row.request_hash,
        record_ids=[UUID(value) for value in row.record_ids],
        inserted_count=row.inserted_count,
        created_at=row.created_at,
    )


def _to_collection_event(row: CollectionEventRow) -> StoredCollectionEvent:
    envelope = CollectionEvent.model_validate(
        {
            "schemaVersion": row.payload["schemaVersion"],
            "eventId": row.id,
            "eventType": row.event_type,
            "occurredAt": row.occurred_at,
            "collectionJobId": row.collection_job_id,
            "externalRequestId": row.payload["externalRequestId"],
            "jobVersion": row.payload["jobVersion"],
            "payload": row.payload["payload"],
        }
    )
    return StoredCollectionEvent(
        id=envelope.event_id,
        schema_version=envelope.schema_version,
        collection_job_id=envelope.collection_job_id,
        external_request_id=envelope.external_request_id,
        event_type=envelope.event_type,
        job_version=envelope.job_version,
        payload=envelope.payload,
        occurred_at=envelope.occurred_at,
    )


def _to_callback_config(
    row: CollectionJobCallbackConfigRow,
) -> CollectionJobCallbackConfig:
    return CollectionJobCallbackConfig(
        id=row.id,
        collection_job_id=row.collection_job_id,
        target_url=row.target_url,
        auth_scheme=row.auth_scheme,
        token_ciphertext=row.token_ciphertext,
        token_nonce=row.token_nonce,
        token_fingerprint=row.token_fingerprint,
        token_expires_at=row.token_expires_at,
        credential_version=row.credential_version,
        encryption_key_version=row.encryption_key_version,
        credential_destroyed_at=row.credential_destroyed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_callback_credential_rotation(
    row: CollectionJobCallbackCredentialRotationRow,
) -> CollectionJobCallbackCredentialRotation:
    return CollectionJobCallbackCredentialRotation(
        id=row.id,
        callback_config_id=row.callback_config_id,
        idempotency_key_hash=row.idempotency_key_hash,
        request_fingerprint=row.request_fingerprint,
        result_credential_version=row.result_credential_version,
        token_expires_at=row.token_expires_at,
        created_at=row.created_at,
    )


class SqlAlchemyApiClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, client: ApiClient) -> None:
        self._session.add(
            ApiClientRow(
                id=client.id,
                tenant_id=client.tenant_id,
                display_name=client.display_name,
                allowed_scopes=[scope.value for scope in client.allowed_scopes],
                config_fingerprint=client.config_fingerprint,
                status=client.status,
                last_token_issued_at=client.last_token_issued_at,
            )
        )

    async def get_by_tenant_id(self, tenant_id: str) -> ApiClient | None:
        row = await self._session.scalar(
            select(ApiClientRow).where(ApiClientRow.tenant_id == tenant_id)
        )
        return _to_api_client(row) if row is not None else None

    async def get_for_update(self, client_id: UUID) -> ApiClient | None:
        row = await self._session.scalar(
            select(ApiClientRow).where(ApiClientRow.id == client_id).with_for_update()
        )
        return _to_api_client(row) if row is not None else None

    async def get_for_share(self, client_id: UUID) -> ApiClient | None:
        row = await self._session.scalar(
            select(ApiClientRow).where(ApiClientRow.id == client_id).with_for_update(read=True)
        )
        return _to_api_client(row) if row is not None else None

    async def list_all(self) -> list[ApiClient]:
        rows = (
            await self._session.scalars(select(ApiClientRow).order_by(ApiClientRow.tenant_id))
        ).all()
        return [_to_api_client(row) for row in rows]

    async def update(self, client: ApiClient) -> None:
        await self._session.execute(
            update(ApiClientRow)
            .where(ApiClientRow.id == client.id)
            .values(
                display_name=client.display_name,
                allowed_scopes=[scope.value for scope in client.allowed_scopes],
                config_fingerprint=client.config_fingerprint,
                status=client.status,
                last_token_issued_at=client.last_token_issued_at,
                updated_at=func.now(),
            )
        )


class SqlAlchemyApiAccessTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: ApiAccessToken) -> None:
        self._session.add(
            ApiAccessTokenRow(
                id=token.id,
                client_id=token.client_id,
                token_hash=token.token_hash,
                issued_via_key_fingerprint=token.issued_via_key_fingerprint,
                scopes=[scope.value for scope in token.scopes],
                issued_at=token.issued_at,
                expires_at=token.expires_at,
                last_used_at=token.last_used_at,
                revoked_at=token.revoked_at,
            )
        )

    async def get(self, token_id: UUID) -> ApiAccessToken | None:
        row = await self._session.get(ApiAccessTokenRow, token_id, populate_existing=True)
        return _to_api_access_token(row) if row is not None else None

    async def get_by_token_hash(self, token_hash: str) -> ApiAccessToken | None:
        row = await self._session.scalar(
            select(ApiAccessTokenRow).where(ApiAccessTokenRow.token_hash == token_hash)
        )
        return _to_api_access_token(row) if row is not None else None

    async def update_last_used(self, token_id: UUID, last_used_at: datetime) -> None:
        await self._session.execute(
            update(ApiAccessTokenRow)
            .where(ApiAccessTokenRow.id == token_id)
            .values(last_used_at=last_used_at)
        )

    async def revoke_active_for_clients(self, client_ids: set[UUID]) -> int:
        if not client_ids:
            return 0
        revoked_ids = await self._session.scalars(
            update(ApiAccessTokenRow)
            .where(
                ApiAccessTokenRow.client_id.in_(client_ids),
                ApiAccessTokenRow.revoked_at.is_(None),
                ApiAccessTokenRow.expires_at > func.now(),
            )
            .values(revoked_at=func.now())
            .returning(ApiAccessTokenRow.id)
        )
        return len(revoked_ids.all())


class SqlAlchemyCollectionAgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, agent: CollectionAgent) -> None:
        self._session.add(
            CollectionAgentRow(
                id=agent.id,
                device_credential_hash=agent.device_credential_hash,
                display_name=agent.display_name,
                capabilities=agent.capabilities.model_dump(mode="json", by_alias=True),
                heartbeat_at=agent.heartbeat_at,
                status=agent.status,
                current_task_id=agent.current_task_id,
            )
        )

    async def get(self, agent_id: UUID) -> CollectionAgent | None:
        row = await self._session.get(CollectionAgentRow, agent_id)
        return _to_agent(row) if row is not None else None

    async def get_for_update(self, agent_id: UUID) -> CollectionAgent | None:
        row = await self._session.scalar(
            select(CollectionAgentRow).where(CollectionAgentRow.id == agent_id).with_for_update()
        )
        return _to_agent(row) if row is not None else None

    async def update(self, agent: CollectionAgent) -> None:
        await self._session.execute(
            update(CollectionAgentRow)
            .where(CollectionAgentRow.id == agent.id)
            .values(
                device_credential_hash=agent.device_credential_hash,
                display_name=agent.display_name,
                capabilities=agent.capabilities.model_dump(mode="json", by_alias=True),
                heartbeat_at=agent.heartbeat_at,
                status=agent.status,
                current_task_id=agent.current_task_id,
                updated_at=func.now(),
            )
        )

    async def clear_current_task(self, agent_id: UUID, task_id: UUID, updated_at: datetime) -> bool:
        cleared_id = await self._session.scalar(
            update(CollectionAgentRow)
            .where(
                CollectionAgentRow.id == agent_id,
                CollectionAgentRow.current_task_id == task_id,
            )
            .values(current_task_id=None, updated_at=updated_at)
            .returning(CollectionAgentRow.id)
        )
        return cleared_id is not None


class SqlAlchemyAgentSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: AgentSession) -> None:
        self._session.add(
            AgentSessionRow(
                id=session.id,
                agent_id=session.agent_id,
                session_token_hash=session.session_token_hash,
                issued_at=session.issued_at,
                expires_at=session.expires_at,
                last_used_at=session.last_used_at,
                revoked_at=session.revoked_at,
            )
        )

    async def get(self, session_id: UUID) -> AgentSession | None:
        row = await self._session.get(AgentSessionRow, session_id, populate_existing=True)
        return _to_agent_session(row) if row is not None else None

    async def get_by_token_hash(self, token_hash: str) -> AgentSession | None:
        row = await self._session.scalar(
            select(AgentSessionRow).where(AgentSessionRow.session_token_hash == token_hash)
        )
        return _to_agent_session(row) if row is not None else None

    async def update_last_used(self, session_id: UUID, last_used_at: datetime) -> None:
        await self._session.execute(
            update(AgentSessionRow)
            .where(AgentSessionRow.id == session_id)
            .values(last_used_at=last_used_at)
        )

    async def revoke_active_for_agent(self, agent_id: UUID, revoked_at: datetime) -> int:
        revoked_ids = await self._session.scalars(
            update(AgentSessionRow)
            .where(
                AgentSessionRow.agent_id == agent_id,
                AgentSessionRow.revoked_at.is_(None),
                AgentSessionRow.expires_at > revoked_at,
            )
            .values(revoked_at=revoked_at)
            .returning(AgentSessionRow.id)
        )
        return len(revoked_ids.all())


class SqlAlchemyBrowserProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_agent(self, agent_id: UUID) -> list[BrowserProfile]:
        rows = (
            await self._session.scalars(
                select(BrowserProfileRow)
                .where(BrowserProfileRow.agent_id == agent_id)
                .order_by(BrowserProfileRow.source, BrowserProfileRow.profile_key)
            )
        ).all()
        return [_to_profile(row) for row in rows]

    async def get(self, profile_id: UUID) -> BrowserProfile | None:
        row = await self._session.get(BrowserProfileRow, profile_id)
        return _to_profile(row) if row is not None else None

    async def upsert(self, profile: BrowserProfile) -> None:
        statement = insert(BrowserProfileRow).values(
            id=profile.id,
            agent_id=profile.agent_id,
            source=profile.source.value,
            profile_key=profile.profile_key,
            status=profile.status,
            updated_at=profile.updated_at or func.now(),
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_browser_profiles_agent_source_key",
                set_={
                    "status": statement.excluded.status,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )


class SqlAlchemyCollectionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, job: CollectionJob) -> None:
        self._session.add(
            CollectionJobRow(
                id=job.id,
                client_id=job.client_id,
                external_request_id=job.external_request_id,
                idempotency_key_hash=job.idempotency_key_hash,
                request_payload=job.request_payload.model_dump(mode="json", by_alias=True),
                desired_state=job.desired_state.value,
                status=job.status.value,
                version=job.version,
                completed_at=job.completed_at,
            )
        )

    async def try_add(self, job: CollectionJob) -> bool:
        inserted_id = await self._session.scalar(
            insert(CollectionJobRow)
            .values(
                id=job.id,
                client_id=job.client_id,
                external_request_id=job.external_request_id,
                idempotency_key_hash=job.idempotency_key_hash,
                request_payload=job.request_payload.model_dump(mode="json", by_alias=True),
                desired_state=job.desired_state.value,
                status=job.status.value,
                version=job.version,
                completed_at=job.completed_at,
            )
            .on_conflict_do_nothing()
            .returning(CollectionJobRow.id)
        )
        return inserted_id is not None

    async def get_for_client(self, job_id: UUID, client_id: UUID) -> CollectionJob | None:
        row = await self._session.scalar(
            select(CollectionJobRow).where(
                CollectionJobRow.id == job_id,
                CollectionJobRow.client_id == client_id,
            )
        )
        return _to_job(row) if row is not None else None

    async def get_for_client_for_update(
        self, job_id: UUID, client_id: UUID
    ) -> CollectionJob | None:
        row = await self._session.scalar(
            select(CollectionJobRow)
            .where(
                CollectionJobRow.id == job_id,
                CollectionJobRow.client_id == client_id,
            )
            .with_for_update()
        )
        return _to_job(row) if row is not None else None

    async def get_for_update(self, job_id: UUID) -> CollectionJob | None:
        row = await self._session.scalar(
            select(CollectionJobRow).where(CollectionJobRow.id == job_id).with_for_update()
        )
        return _to_job(row) if row is not None else None

    async def lock_next_claimable(self, sources: list[SourceCode]) -> CollectionJob | None:
        claimable_task_exists = exists(
            select(CollectionTaskRow.id).where(
                CollectionTaskRow.collection_job_id == CollectionJobRow.id,
                CollectionTaskRow.source.in_([source.value for source in sources]),
                CollectionTaskRow.desired_state == DesiredState.RUNNING.value,
                CollectionTaskRow.status == CollectionState.QUEUED.value,
                CollectionTaskRow.leased_agent_id.is_(None),
                CollectionTaskRow.leased_profile_id.is_(None),
                CollectionTaskRow.lease_token_hash.is_(None),
                CollectionTaskRow.lease_expires_at.is_(None),
            )
        )
        row = await self._session.scalar(
            select(CollectionJobRow)
            .where(
                CollectionJobRow.desired_state == DesiredState.RUNNING.value,
                CollectionJobRow.status.in_(
                    [CollectionState.QUEUED.value, CollectionState.RUNNING.value]
                ),
                claimable_task_exists,
            )
            .order_by(CollectionJobRow.created_at, CollectionJobRow.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return _to_job(row) if row is not None else None

    async def find_next_with_expired_lease(self, expired_at: datetime) -> CollectionJob | None:
        expired_task_exists = exists(
            select(CollectionTaskRow.id).where(
                CollectionTaskRow.collection_job_id == CollectionJobRow.id,
                CollectionTaskRow.leased_agent_id.is_not(None),
                CollectionTaskRow.lease_expires_at <= expired_at,
                CollectionTaskRow.status.in_(
                    [
                        CollectionState.RUNNING.value,
                        CollectionState.AWAITING_HUMAN.value,
                        CollectionState.PAUSING.value,
                        CollectionState.CANCELLING.value,
                    ]
                ),
            )
        )
        row = await self._session.scalar(
            select(CollectionJobRow)
            .where(expired_task_exists)
            .order_by(CollectionJobRow.created_at, CollectionJobRow.id)
            .limit(1)
        )
        return _to_job(row) if row is not None else None

    async def get_by_external_request_id(
        self, client_id: UUID, external_request_id: str
    ) -> CollectionJob | None:
        row = await self._session.scalar(
            select(CollectionJobRow).where(
                CollectionJobRow.client_id == client_id,
                CollectionJobRow.external_request_id == external_request_id,
            )
        )
        return _to_job(row) if row is not None else None

    async def get_by_idempotency_hash(
        self, client_id: UUID, idempotency_key_hash: str
    ) -> CollectionJob | None:
        row = await self._session.scalar(
            select(CollectionJobRow).where(
                CollectionJobRow.client_id == client_id,
                CollectionJobRow.idempotency_key_hash == idempotency_key_hash,
            )
        )
        return _to_job(row) if row is not None else None

    async def update_control_state(self, job: CollectionJob, updated_at: datetime) -> None:
        await self._session.execute(
            update(CollectionJobRow)
            .where(CollectionJobRow.id == job.id)
            .values(
                desired_state=job.desired_state.value,
                status=job.status.value,
                version=job.version,
                updated_at=updated_at,
                completed_at=job.completed_at,
            )
        )

    async def update_runtime_state(self, job: CollectionJob, updated_at: datetime) -> None:
        await self._session.execute(
            update(CollectionJobRow)
            .where(CollectionJobRow.id == job.id)
            .values(
                status=job.status.value,
                version=job.version,
                updated_at=updated_at,
                completed_at=job.completed_at,
            )
        )


class SqlAlchemyCollectionTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: CollectionTask) -> None:
        self._session.add(
            CollectionTaskRow(
                id=task.id,
                collection_job_id=task.collection_job_id,
                source=task.source.value,
                keyword=task.keyword,
                region=task.region,
                page_limit=task.page_limit,
                page_checkpoint=task.page_checkpoint,
                desired_state=task.desired_state.value,
                status=task.status.value,
                leased_agent_id=task.leased_agent_id,
                leased_profile_id=task.leased_profile_id,
                lease_token_hash=task.lease_token_hash,
                fencing_token=task.fencing_token,
                lease_expires_at=task.lease_expires_at,
                attempt_count=task.attempt_count,
                processed_count=task.processed_count,
                failure_code=task.failure_code,
            )
        )

    async def get(self, task_id: UUID) -> CollectionTask | None:
        row = await self._session.get(CollectionTaskRow, task_id)
        return _to_task(row) if row is not None else None

    async def get_for_update(self, task_id: UUID) -> CollectionTask | None:
        row = await self._session.scalar(
            select(CollectionTaskRow).where(CollectionTaskRow.id == task_id).with_for_update()
        )
        return _to_task(row) if row is not None else None

    async def lock_next_claimable(
        self, job_id: UUID, sources: list[SourceCode]
    ) -> CollectionTask | None:
        row = await self._session.scalar(
            select(CollectionTaskRow)
            .where(
                CollectionTaskRow.collection_job_id == job_id,
                CollectionTaskRow.source.in_([source.value for source in sources]),
                CollectionTaskRow.desired_state == DesiredState.RUNNING.value,
                CollectionTaskRow.status == CollectionState.QUEUED.value,
                CollectionTaskRow.leased_agent_id.is_(None),
                CollectionTaskRow.leased_profile_id.is_(None),
                CollectionTaskRow.lease_token_hash.is_(None),
                CollectionTaskRow.lease_expires_at.is_(None),
            )
            .order_by(CollectionTaskRow.created_at, CollectionTaskRow.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return _to_task(row) if row is not None else None

    async def find_expired_for_job(
        self, job_id: UUID, expired_at: datetime
    ) -> CollectionTask | None:
        row = await self._session.scalar(
            select(CollectionTaskRow)
            .where(
                CollectionTaskRow.collection_job_id == job_id,
                CollectionTaskRow.leased_agent_id.is_not(None),
                CollectionTaskRow.lease_expires_at <= expired_at,
                CollectionTaskRow.status.in_(
                    [
                        CollectionState.RUNNING.value,
                        CollectionState.AWAITING_HUMAN.value,
                        CollectionState.PAUSING.value,
                        CollectionState.CANCELLING.value,
                    ]
                ),
            )
            .order_by(CollectionTaskRow.lease_expires_at, CollectionTaskRow.id)
            .limit(1)
        )
        return _to_task(row) if row is not None else None

    async def list_for_job(self, job_id: UUID) -> list[CollectionTask]:
        rows = (
            await self._session.scalars(
                select(CollectionTaskRow)
                .where(CollectionTaskRow.collection_job_id == job_id)
                .order_by(CollectionTaskRow.id)
            )
        ).all()
        return [_to_task(row) for row in rows]

    async def list_for_job_for_update(self, job_id: UUID) -> list[CollectionTask]:
        rows = (
            await self._session.scalars(
                select(CollectionTaskRow)
                .where(CollectionTaskRow.collection_job_id == job_id)
                .order_by(CollectionTaskRow.id)
                .with_for_update()
            )
        ).all()
        return [_to_task(row) for row in rows]

    async def update_control_state(self, task: CollectionTask, updated_at: datetime) -> None:
        await self._session.execute(
            update(CollectionTaskRow)
            .where(CollectionTaskRow.id == task.id)
            .values(
                desired_state=task.desired_state.value,
                status=task.status.value,
                updated_at=updated_at,
            )
        )

    async def update_lease_state(self, task: CollectionTask, updated_at: datetime) -> None:
        await self._session.execute(
            update(CollectionTaskRow)
            .where(CollectionTaskRow.id == task.id)
            .values(
                status=task.status.value,
                leased_agent_id=task.leased_agent_id,
                leased_profile_id=task.leased_profile_id,
                lease_token_hash=task.lease_token_hash,
                fencing_token=task.fencing_token,
                lease_expires_at=task.lease_expires_at,
                attempt_count=task.attempt_count,
                failure_code=task.failure_code,
                updated_at=updated_at,
            )
        )

    async def update_runtime_state(self, task: CollectionTask, updated_at: datetime) -> None:
        await self._session.execute(
            update(CollectionTaskRow)
            .where(CollectionTaskRow.id == task.id)
            .values(
                page_checkpoint=task.page_checkpoint,
                desired_state=task.desired_state.value,
                status=task.status.value,
                leased_agent_id=task.leased_agent_id,
                leased_profile_id=task.leased_profile_id,
                lease_token_hash=task.lease_token_hash,
                fencing_token=task.fencing_token,
                lease_expires_at=task.lease_expires_at,
                attempt_count=task.attempt_count,
                processed_count=task.processed_count,
                failure_code=task.failure_code,
                updated_at=updated_at,
            )
        )


class SqlAlchemyCollectionTaskAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, attempt: CollectionTaskAttempt) -> None:
        self._session.add(
            CollectionTaskAttemptRow(
                id=attempt.id,
                collection_task_id=attempt.collection_task_id,
                agent_id=attempt.agent_id,
                fencing_token=attempt.fencing_token,
                claimed_at=attempt.claimed_at,
                released_at=attempt.released_at,
                outcome=attempt.outcome.value if attempt.outcome is not None else None,
                failure_code=attempt.failure_code,
            )
        )

    async def get(self, attempt_id: UUID) -> CollectionTaskAttempt | None:
        row = await self._session.get(CollectionTaskAttemptRow, attempt_id)
        return _to_task_attempt(row) if row is not None else None

    async def get_by_task_fencing(
        self, task_id: UUID, fencing_token: int
    ) -> CollectionTaskAttempt | None:
        row = await self._session.scalar(
            select(CollectionTaskAttemptRow).where(
                CollectionTaskAttemptRow.collection_task_id == task_id,
                CollectionTaskAttemptRow.fencing_token == fencing_token,
            )
        )
        return _to_task_attempt(row) if row is not None else None

    async def update_outcome(self, attempt: CollectionTaskAttempt) -> None:
        await self._session.execute(
            update(CollectionTaskAttemptRow)
            .where(CollectionTaskAttemptRow.id == attempt.id)
            .values(
                released_at=attempt.released_at,
                outcome=attempt.outcome.value if attempt.outcome is not None else None,
                failure_code=attempt.failure_code,
            )
        )


class SqlAlchemyParsedJobRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(
        self, records: list[StoredParsedJobRecord], created_at: datetime
    ) -> tuple[list[StoredParsedJobRecord], int]:
        if not records:
            return [], 0
        inserted_ids = set(
            (
                await self._session.scalars(
                    insert(ParsedJobRecordRow)
                    .values(
                        [
                            {
                                "id": record.id,
                                "collection_task_id": record.collection_task_id,
                                "source_job_id": record.source_job_id,
                                "source_url": record.source_url,
                                "query_context": {
                                    "keyword": record.keyword,
                                    "region": record.region,
                                    "page": record.page,
                                },
                                "parsed_fields": record.fields.model_dump(
                                    mode="json", by_alias=True
                                ),
                                "payload_hash": record.payload_hash,
                                "collected_at": record.collected_at,
                                "created_at": created_at,
                            }
                            for record in records
                        ]
                    )
                    .on_conflict_do_nothing(constraint="uq_parsed_job_records_task_url_payload")
                    .returning(ParsedJobRecordRow.id)
                )
            ).all()
        )
        task_id = records[0].collection_task_id
        identities = [(record.source_url, record.payload_hash) for record in records]
        rows = (
            await self._session.scalars(
                select(ParsedJobRecordRow).where(
                    ParsedJobRecordRow.collection_task_id == task_id,
                    tuple_(
                        ParsedJobRecordRow.source_url,
                        ParsedJobRecordRow.payload_hash,
                    ).in_(identities),
                )
            )
        ).all()
        source = records[0].source.value
        by_identity = {
            (row.source_url, row.payload_hash): _to_parsed_record(row, source) for row in rows
        }
        stored = [by_identity[(record.source_url, record.payload_hash)] for record in records]
        return stored, len(inserted_ids)

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        after_created_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredParsedJobRecord]:
        statement = (
            select(ParsedJobRecordRow, CollectionTaskRow.source)
            .join(
                CollectionTaskRow,
                CollectionTaskRow.id == ParsedJobRecordRow.collection_task_id,
            )
            .where(CollectionTaskRow.collection_job_id == job_id)
        )
        if after_created_at is not None and after_id is not None:
            statement = statement.where(
                or_(
                    ParsedJobRecordRow.created_at > after_created_at,
                    and_(
                        ParsedJobRecordRow.created_at == after_created_at,
                        ParsedJobRecordRow.id > after_id,
                    ),
                )
            )
        rows = (
            await self._session.execute(
                statement.order_by(ParsedJobRecordRow.created_at, ParsedJobRecordRow.id).limit(
                    limit
                )
            )
        ).all()
        return [_to_parsed_record(row, source) for row, source in rows]


class SqlAlchemyParsedRecordBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, batch_id: UUID) -> ParsedRecordBatchReceipt | None:
        row = await self._session.get(ParsedRecordBatchRow, batch_id)
        return _to_batch_receipt(row) if row is not None else None

    async def try_add(self, receipt: ParsedRecordBatchReceipt) -> bool:
        inserted_id = await self._session.scalar(
            insert(ParsedRecordBatchRow)
            .values(
                id=receipt.id,
                collection_task_id=receipt.collection_task_id,
                attempt_id=receipt.attempt_id,
                request_hash=receipt.request_hash,
                record_ids=[str(record_id) for record_id in receipt.record_ids],
                inserted_count=receipt.inserted_count,
                created_at=receipt.created_at or func.now(),
            )
            .on_conflict_do_nothing(index_elements=[ParsedRecordBatchRow.id])
            .returning(ParsedRecordBatchRow.id)
        )
        return inserted_id is not None


class SqlAlchemyCollectionEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: StoredCollectionEvent) -> None:
        self._session.add(
            CollectionEventRow(
                id=event.id,
                collection_job_id=event.collection_job_id,
                event_type=event.event_type.value,
                payload={
                    "schemaVersion": event.schema_version,
                    "externalRequestId": event.external_request_id,
                    "jobVersion": event.job_version,
                    "payload": event.payload.model_dump(mode="json", by_alias=True),
                },
                occurred_at=event.occurred_at,
            )
        )

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        after_occurred_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredCollectionEvent]:
        conditions = [CollectionEventRow.collection_job_id == job_id]
        if after_occurred_at is not None and after_id is not None:
            conditions.append(
                tuple_(CollectionEventRow.occurred_at, CollectionEventRow.id)
                > (after_occurred_at, after_id)
            )
        rows = (
            await self._session.scalars(
                select(CollectionEventRow)
                .where(*conditions)
                .order_by(CollectionEventRow.occurred_at, CollectionEventRow.id)
                .limit(limit)
            )
        ).all()
        return [_to_collection_event(row) for row in rows]


class SqlAlchemyCollectionJobCallbackConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, config: CollectionJobCallbackConfig) -> None:
        self._session.add(
            CollectionJobCallbackConfigRow(
                id=config.id,
                collection_job_id=config.collection_job_id,
                target_url=config.target_url,
                auth_scheme=config.auth_scheme,
                token_ciphertext=config.token_ciphertext,
                token_nonce=config.token_nonce,
                token_fingerprint=config.token_fingerprint,
                token_expires_at=config.token_expires_at,
                credential_version=config.credential_version,
                encryption_key_version=config.encryption_key_version,
            )
        )

    async def get_for_job(self, job_id: UUID) -> CollectionJobCallbackConfig | None:
        row = await self._session.scalar(
            select(CollectionJobCallbackConfigRow).where(
                CollectionJobCallbackConfigRow.collection_job_id == job_id
            )
        )
        return _to_callback_config(row) if row is not None else None

    async def update_credentials(
        self,
        config: CollectionJobCallbackConfig,
        *,
        expected_credential_version: int,
        updated_at: datetime,
    ) -> bool:
        updated_id = await self._session.scalar(
            update(CollectionJobCallbackConfigRow)
            .where(
                CollectionJobCallbackConfigRow.id == config.id,
                CollectionJobCallbackConfigRow.credential_version == expected_credential_version,
                CollectionJobCallbackConfigRow.credential_destroyed_at.is_(None),
            )
            .values(
                token_ciphertext=config.token_ciphertext,
                token_nonce=config.token_nonce,
                token_fingerprint=config.token_fingerprint,
                token_expires_at=config.token_expires_at,
                credential_version=config.credential_version,
                encryption_key_version=config.encryption_key_version,
                updated_at=updated_at,
            )
            .returning(CollectionJobCallbackConfigRow.id)
        )
        return updated_id is not None


class SqlAlchemyCollectionJobCallbackCredentialRotationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, rotation: CollectionJobCallbackCredentialRotation) -> None:
        self._session.add(
            CollectionJobCallbackCredentialRotationRow(
                id=rotation.id,
                callback_config_id=rotation.callback_config_id,
                idempotency_key_hash=rotation.idempotency_key_hash,
                request_fingerprint=rotation.request_fingerprint,
                result_credential_version=rotation.result_credential_version,
                token_expires_at=rotation.token_expires_at,
                created_at=rotation.created_at or func.now(),
            )
        )

    async def get(
        self, callback_config_id: UUID, idempotency_key_hash: str
    ) -> CollectionJobCallbackCredentialRotation | None:
        row = await self._session.scalar(
            select(CollectionJobCallbackCredentialRotationRow).where(
                CollectionJobCallbackCredentialRotationRow.callback_config_id == callback_config_id,
                CollectionJobCallbackCredentialRotationRow.idempotency_key_hash
                == idempotency_key_hash,
            )
        )
        return _to_callback_credential_rotation(row) if row is not None else None


class SqlAlchemyWebhookOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: WebhookOutboxEntry) -> None:
        self._session.add(
            WebhookOutboxRow(
                id=entry.id,
                event_id=entry.event_id,
                callback_config_id=entry.callback_config_id,
                status=entry.status.value,
                attempt_count=entry.attempt_count,
                next_attempt_at=entry.next_attempt_at,
                delivered_at=entry.delivered_at,
                last_failure_code=entry.last_failure_code,
                created_at=entry.created_at or func.now(),
            )
        )

    async def has_undelivered_for_callback(self, callback_config_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(
                    exists().where(
                        WebhookOutboxRow.callback_config_id == callback_config_id,
                        WebhookOutboxRow.delivered_at.is_(None),
                    )
                )
            )
        )

    async def resume_credential_blocked(
        self, callback_config_id: UUID, resumed_at: datetime
    ) -> int:
        resumed_ids = (
            await self._session.scalars(
                update(WebhookOutboxRow)
                .where(
                    WebhookOutboxRow.callback_config_id == callback_config_id,
                    WebhookOutboxRow.status == WebhookOutboxStatus.CREDENTIAL_BLOCKED.value,
                    WebhookOutboxRow.delivered_at.is_(None),
                )
                .values(
                    status=WebhookOutboxStatus.PENDING.value,
                    next_attempt_at=resumed_at,
                    last_failure_code=None,
                )
                .returning(WebhookOutboxRow.id)
            )
        ).all()
        return len(resumed_ids)

    async def claim_due(
        self,
        *,
        claim_id: UUID,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> ClaimedWebhookDelivery | None:
        row = await self._session.scalar(
            select(WebhookOutboxRow)
            .where(
                WebhookOutboxRow.status == WebhookOutboxStatus.PENDING.value,
                WebhookOutboxRow.next_attempt_at <= claimed_at,
                or_(
                    WebhookOutboxRow.dispatch_claim_id.is_(None),
                    WebhookOutboxRow.dispatch_lease_expires_at <= claimed_at,
                ),
            )
            .order_by(WebhookOutboxRow.next_attempt_at, WebhookOutboxRow.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None

        event_row = await self._session.get(CollectionEventRow, row.event_id)
        callback_row = await self._session.get(
            CollectionJobCallbackConfigRow, row.callback_config_id
        )
        if event_row is None or callback_row is None:
            raise RuntimeError("webhook Outbox references are inconsistent")

        recovered_expired_claim = row.dispatch_claim_id is not None
        row.dispatch_claim_id = claim_id
        row.dispatch_claimed_at = claimed_at
        row.dispatch_lease_expires_at = lease_expires_at
        return ClaimedWebhookDelivery(
            outbox_id=row.id,
            claim_id=claim_id,
            event=_to_collection_event(event_row),
            callback_config=_to_callback_config(callback_row),
            attempt_number=row.attempt_count + 1,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            recovered_expired_claim=recovered_expired_claim,
        )

    async def settle_claim(
        self, settlement: WebhookDeliverySettlement
    ) -> WebhookDeliverySettlement | None:
        row = await self._session.scalar(
            select(WebhookOutboxRow)
            .where(
                WebhookOutboxRow.id == settlement.outbox_id,
                WebhookOutboxRow.status == WebhookOutboxStatus.PENDING.value,
                WebhookOutboxRow.dispatch_claim_id == settlement.claim_id,
            )
            .with_for_update()
        )
        if row is None:
            return None

        callback_row = await self._session.scalar(
            select(CollectionJobCallbackConfigRow)
            .where(CollectionJobCallbackConfigRow.id == row.callback_config_id)
            .with_for_update()
        )
        if callback_row is None:
            raise RuntimeError("webhook Outbox callback configuration is missing")

        status = settlement.status
        next_attempt_at = settlement.next_attempt_at
        failure_code = settlement.failure_code
        if (
            status is WebhookOutboxStatus.CREDENTIAL_BLOCKED
            and callback_row.credential_version != settlement.claimed_credential_version
        ):
            status = WebhookOutboxStatus.PENDING
            next_attempt_at = settlement.settled_at
            failure_code = None

        if status is WebhookOutboxStatus.PENDING and next_attempt_at is None:
            raise ValueError("pending webhook settlement requires next_attempt_at")

        row.status = status.value
        row.attempt_count += int(settlement.attempted)
        row.next_attempt_at = next_attempt_at or row.next_attempt_at
        row.delivered_at = (
            settlement.settled_at if status is WebhookOutboxStatus.DELIVERED else None
        )
        row.last_failure_code = failure_code
        row.dispatch_claim_id = None
        row.dispatch_claimed_at = None
        row.dispatch_lease_expires_at = None

        if (
            status is WebhookOutboxStatus.DELIVERED
            and callback_row.credential_destroyed_at is None
        ):
            job_is_terminal = bool(
                await self._session.scalar(
                    select(
                        exists().where(
                            CollectionJobRow.id == callback_row.collection_job_id,
                            CollectionJobRow.status.in_(
                                [state.value for state in TERMINAL_COLLECTION_STATES]
                            ),
                        )
                    )
                )
            )
            has_other_undelivered = bool(
                await self._session.scalar(
                    select(
                        exists().where(
                            WebhookOutboxRow.callback_config_id == row.callback_config_id,
                            WebhookOutboxRow.id != row.id,
                            WebhookOutboxRow.status
                            != WebhookOutboxStatus.DELIVERED.value,
                        )
                    )
                )
            )
            if job_is_terminal and not has_other_undelivered:
                callback_row.token_ciphertext = None
                callback_row.token_nonce = None
                callback_row.credential_destroyed_at = settlement.settled_at
                callback_row.updated_at = settlement.settled_at
        return replace(
            settlement,
            status=status,
            next_attempt_at=next_attempt_at,
            failure_code=failure_code,
        )

    async def get_metrics_snapshot(
        self, observed_at: datetime
    ) -> WebhookQueueMetricsSnapshot:
        row = (
            await self._session.execute(
                select(
                    func.count()
                    .filter(WebhookOutboxRow.status == WebhookOutboxStatus.PENDING.value)
                    .label("pending_count"),
                    func.min(WebhookOutboxRow.created_at)
                    .filter(WebhookOutboxRow.status == WebhookOutboxStatus.PENDING.value)
                    .label("oldest_pending_created_at"),
                    func.count()
                    .filter(
                        WebhookOutboxRow.status
                        == WebhookOutboxStatus.CREDENTIAL_BLOCKED.value
                    )
                    .label("credential_blocked_count"),
                    func.count()
                    .filter(
                        WebhookOutboxRow.status
                        == WebhookOutboxStatus.PERMANENTLY_FAILED.value
                    )
                    .label("permanently_failed_count"),
                    func.count()
                    .filter(
                        WebhookOutboxRow.status
                        == WebhookOutboxStatus.PERMANENTLY_FAILED.value,
                        WebhookOutboxRow.last_failure_code.like("retry_exhausted_%"),
                    )
                    .label("retry_exhausted_count"),
                    func.count()
                    .filter(
                        WebhookOutboxRow.status == WebhookOutboxStatus.PENDING.value,
                        WebhookOutboxRow.dispatch_claim_id.is_not(None),
                        WebhookOutboxRow.dispatch_lease_expires_at > observed_at,
                    )
                    .label("active_claim_count"),
                )
            )
        ).one()
        return WebhookQueueMetricsSnapshot(
            pending_count=int(row.pending_count),
            oldest_pending_created_at=row.oldest_pending_created_at,
            credential_blocked_count=int(row.credential_blocked_count),
            permanently_failed_count=int(row.permanently_failed_count),
            retry_exhausted_count=int(row.retry_exhausted_count),
            active_claim_count=int(row.active_claim_count),
        )

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        status: WebhookOutboxStatus,
        after_created_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredWebhookDelivery]:
        statement = (
            select(
                WebhookOutboxRow,
                CollectionEventRow,
                CollectionJobCallbackConfigRow,
            )
            .join(CollectionEventRow, CollectionEventRow.id == WebhookOutboxRow.event_id)
            .join(
                CollectionJobCallbackConfigRow,
                CollectionJobCallbackConfigRow.id == WebhookOutboxRow.callback_config_id,
            )
            .where(
                CollectionEventRow.collection_job_id == job_id,
                WebhookOutboxRow.status == status.value,
            )
        )
        if after_created_at is not None and after_id is not None:
            statement = statement.where(
                tuple_(WebhookOutboxRow.created_at, WebhookOutboxRow.id)
                > (after_created_at, after_id)
            )
        rows = (
            await self._session.execute(
                statement.order_by(WebhookOutboxRow.created_at, WebhookOutboxRow.id).limit(
                    limit
                )
            )
        ).all()
        return [
            _to_stored_webhook_delivery(outbox, event, callback)
            for outbox, event, callback in rows
        ]

    async def get_for_client_for_update(
        self,
        *,
        outbox_id: UUID,
        job_id: UUID,
        client_id: UUID,
    ) -> StoredWebhookDelivery | None:
        result = (
            await self._session.execute(
                select(WebhookOutboxRow, CollectionEventRow)
                .join(CollectionEventRow, CollectionEventRow.id == WebhookOutboxRow.event_id)
                .join(CollectionJobRow, CollectionJobRow.id == CollectionEventRow.collection_job_id)
                .where(
                    WebhookOutboxRow.id == outbox_id,
                    CollectionEventRow.collection_job_id == job_id,
                    CollectionJobRow.client_id == client_id,
                )
                .with_for_update(of=WebhookOutboxRow)
            )
        ).one_or_none()
        if result is None:
            return None
        outbox, event = result
        callback = await self._session.scalar(
            select(CollectionJobCallbackConfigRow)
            .where(CollectionJobCallbackConfigRow.id == outbox.callback_config_id)
            .with_for_update()
        )
        if callback is None:
            raise RuntimeError("webhook Outbox callback configuration is missing")
        return _to_stored_webhook_delivery(outbox, event, callback)

    async def activate_replay(
        self,
        outbox_id: UUID,
        *,
        expected_replay_count: int,
        replayed_at: datetime,
    ) -> bool:
        updated_id = await self._session.scalar(
            update(WebhookOutboxRow)
            .where(
                WebhookOutboxRow.id == outbox_id,
                WebhookOutboxRow.status
                == WebhookOutboxStatus.PERMANENTLY_FAILED.value,
                WebhookOutboxRow.last_failure_code.like("retry_exhausted_%"),
                WebhookOutboxRow.replay_count == expected_replay_count,
                WebhookOutboxRow.dispatch_claim_id.is_(None),
                WebhookOutboxRow.dispatch_claimed_at.is_(None),
                WebhookOutboxRow.dispatch_lease_expires_at.is_(None),
            )
            .values(
                status=WebhookOutboxStatus.PENDING.value,
                attempt_count=0,
                replay_count=expected_replay_count + 1,
                last_replayed_at=replayed_at,
                next_attempt_at=replayed_at,
                delivered_at=None,
                last_failure_code=None,
            )
            .returning(WebhookOutboxRow.id)
        )
        return updated_id is not None


class SqlAlchemyWebhookDeliveryReplayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, replay: WebhookDeliveryReplay) -> None:
        self._session.add(
            WebhookOutboxReplayRow(
                id=replay.id,
                webhook_outbox_id=replay.webhook_outbox_id,
                idempotency_key_hash=replay.idempotency_key_hash,
                replay_number=replay.replay_number,
                previous_attempt_count=replay.previous_attempt_count,
                previous_failure_code=replay.previous_failure_code,
                created_at=replay.created_at or func.now(),
            )
        )

    async def get(
        self, webhook_outbox_id: UUID, idempotency_key_hash: str
    ) -> WebhookDeliveryReplay | None:
        row = await self._session.scalar(
            select(WebhookOutboxReplayRow).where(
                WebhookOutboxReplayRow.webhook_outbox_id == webhook_outbox_id,
                WebhookOutboxReplayRow.idempotency_key_hash == idempotency_key_hash,
            )
        )
        return _to_webhook_delivery_replay(row) if row is not None else None

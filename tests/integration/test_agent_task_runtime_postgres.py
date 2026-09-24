import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.agent_authentication import AgentSessionAuthenticator
from crawler_api.application.agent_heartbeats import AgentHeartbeatRecorder
from crawler_api.application.agent_provisioning import AgentProvisioner
from crawler_api.application.agent_sessions import AgentSessionIssuer
from crawler_api.application.agent_task_claims import AgentTaskClaimer
from crawler_api.application.agent_task_runtime import (
    AgentTaskRuntime,
    ExpiredLeaseRecoverer,
)
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import CollectionJobSubmitter
from crawler_api.application.task_leases import (
    ActiveTaskLeaseAuthenticator,
    TaskLeaseStateConflict,
)
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import (
    BrowserProfileRow,
    CollectionAgentRow,
    CollectionJobRow,
    CollectionTaskAttemptRow,
    CollectionTaskRow,
)
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from crawler_api.infrastructure.security.task_lease_tokens import TaskLeaseTokenManager
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.agent_protocol import (
    AgentHeartbeatRequest,
    ClaimTaskRequest,
    CompleteTaskRequest,
    LeaseReference,
    ReleaseTaskRequest,
    TaskCheckpointRequest,
    TaskLease,
)
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "agent-task-runtime-integration-tenant"
TENANT_KEY = "agent-task-runtime-integration-key"
ACCESS_TOKEN = "cat_agent_task_runtime_integration_token"
AGENT_ID = UUID("77777777-aaaa-4777-8777-777777777777")
CLAIM_ONE_ID = UUID("88888888-bbbb-4888-8888-888888888888")
CLAIM_TWO_ID = UUID("99999999-cccc-4999-8999-999999999999")
CLAIM_THREE_ID = UUID("aaaaaaaa-dddd-4aaa-8aaa-aaaaaaaaaaaa")
DEVICE_SECRET = "dev_agent_task_runtime_secret"
SESSION_TOKEN = "agt_agent_task_runtime_session_token"
BASE_TIME = datetime(2026, 9, 17, 14, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def constant_value(value: str) -> Callable[[], str]:
    return lambda: value


def build_allowlist() -> TenantAllowlist:
    key_hash = hashlib.sha256(TENANT_KEY.encode()).hexdigest()
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Agent task runtime integration tenant",
                    "enabled": True,
                    "tenantKeyHashes": [f"sha256:{key_hash}"],
                }
            ],
        }
    )


def build_heartbeat() -> AgentHeartbeatRequest:
    return AgentHeartbeatRequest.model_validate(
        {
            "agentId": str(AGENT_ID),
            "capabilities": {
                "supportedSources": ["zhaopin"],
                "maxConcurrentTasks": 1,
            },
            "availableSlots": 1,
            "profiles": [
                {
                    "source": "zhaopin",
                    "profileKey": "runtime-primary",
                    "status": "ready",
                }
            ],
        }
    )


def lease_reference(lease: TaskLease, expires_at: datetime | None = None) -> LeaseReference:
    return LeaseReference(
        agent_id=lease.agent_id,
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        fencing_token=lease.fencing_token,
        lease_expires_at=expires_at or lease.lease_expires_at,
    )


async def test_runtime_release_recovery_completion_and_full_rollback() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    api_token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    device_hasher = DeviceCredentialHasher(settings.agent_device_credential_hash_key)
    session_hasher = AgentSessionTokenHasher(settings.agent_token_hash_key)
    lease_tokens = TaskLeaseTokenManager(settings.agent_token_hash_key)
    job_id: UUID | None = None
    task_id: UUID | None = None
    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()
            session_factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )

            def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
                return SqlAlchemyUnitOfWork(session_factory)

            allowlist = build_allowlist()
            await TenantAllowlistSynchronizer(unit_of_work_factory).synchronize(
                allowlist
            )
            issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=api_token_hasher.hash,
                ttl_seconds=3600,
                clock=lambda: BASE_TIME,
                token_generator=lambda: ACCESS_TOKEN,
            ).issue(SecretStr(TENANT_KEY))
            submitted = await CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=CallerAccessTokenAuthenticator(api_token_hasher.hash),
                tenant_allowlist=allowlist,
                callback_token_protector=CallbackTokenProtector(
                    encryption_key=settings.callback_token_encryption_key,
                    fingerprint_key=settings.callback_token_fingerprint_key,
                    encryption_key_version=settings.callback_token_encryption_key_version,
                ),
                clock=lambda: BASE_TIME,
            ).submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("agent-runtime-integration-operation"),
                request=SubmitCollectionJobRequest.model_validate(
                    {
                        "externalRequestId": "jobreq_agent_runtime_integration_001",
                        "source": "zhaopin",
                        "queries": [
                            {
                                "keyword": "Python",
                                "region": "Shanghai",
                                "pageLimit": 5,
                            }
                        ],
                    }
                ),
            )
            job_id = submitted.job.id

            await AgentProvisioner(
                unit_of_work_factory=unit_of_work_factory,
                hash_device_secret=device_hasher.hash,
                device_secret_generator=constant_value(DEVICE_SECRET),
            ).provision(display_name="Runtime integration Agent", agent_id=AGENT_ID)
            await AgentSessionIssuer(
                unit_of_work_factory=unit_of_work_factory,
                verify_device_secret=device_hasher.verify,
                hash_session_token=session_hasher.hash,
                ttl_seconds=1800,
                clock=lambda: BASE_TIME,
                token_generator=constant_value(SESSION_TOKEN),
            ).issue(AGENT_ID, SecretStr(DEVICE_SECRET))

            heartbeat_clock = MutableClock(BASE_TIME + timedelta(minutes=1))
            heartbeat_recorder = AgentHeartbeatRecorder(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                profile_offline_seconds=180,
                clock=heartbeat_clock,
            )
            await heartbeat_recorder.record(
                session_token=SecretStr(SESSION_TOKEN),
                heartbeat=build_heartbeat(),
            )

            claim_clock = MutableClock(BASE_TIME + timedelta(minutes=2))
            claimer = AgentTaskClaimer(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                lease_tokens=lease_tokens,
                lease_seconds=120,
                agent_offline_seconds=180,
                clock=claim_clock,
            )
            lease_authenticator = ActiveTaskLeaseAuthenticator(
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                lease_tokens=lease_tokens,
            )
            runtime_clock = MutableClock(claim_clock.now + timedelta(seconds=10))
            runtime = AgentTaskRuntime(
                unit_of_work_factory=unit_of_work_factory,
                lease_authenticator=lease_authenticator,
                lease_seconds=120,
                clock=runtime_clock,
            )

            first = await claimer.claim(
                session_token=SecretStr(SESSION_TOKEN),
                request=ClaimTaskRequest(
                    agent_id=AGENT_ID, claim_request_id=CLAIM_ONE_ID
                ),
            )
            assert first is not None
            task_id = first.task_id
            first_reference = lease_reference(first)
            renewed = await runtime.heartbeat(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=first.task_id,
                request=first_reference,
            )
            runtime_clock.now += timedelta(seconds=10)
            checkpointed = await runtime.checkpoint(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=first.task_id,
                request=TaskCheckpointRequest(
                    **lease_reference(
                        first, renewed.lease_expires_at
                    ).model_dump(),
                    page_checkpoint=2,
                    processed_count=7,
                ),
            )
            runtime_clock.now += timedelta(seconds=10)
            await runtime.release(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=first.task_id,
                request=ReleaseTaskRequest(
                    **lease_reference(
                        first, checkpointed.lease_expires_at
                    ).model_dump(),
                    failure_code="browser_process_exited",
                ),
            )

            claim_clock.now = runtime_clock.now + timedelta(seconds=1)
            second = await claimer.claim(
                session_token=SecretStr(SESSION_TOKEN),
                request=ClaimTaskRequest(
                    agent_id=AGENT_ID, claim_request_id=CLAIM_TWO_ID
                ),
            )
            assert second is not None
            assert second.page_checkpoint == 2
            assert second.fencing_token == 2

            recovery_clock = MutableClock(
                second.lease_expires_at + timedelta(seconds=1)
            )
            recovered = await ExpiredLeaseRecoverer(
                unit_of_work_factory=unit_of_work_factory,
                clock=recovery_clock,
            ).recover_one()
            assert recovered is True
            runtime_clock.now = recovery_clock.now
            with pytest.raises(TaskLeaseStateConflict):
                await runtime.heartbeat(
                    session_token=SecretStr(SESSION_TOKEN),
                    task_id=second.task_id,
                    request=lease_reference(second),
                )

            heartbeat_clock.now = recovery_clock.now + timedelta(seconds=1)
            await heartbeat_recorder.record(
                session_token=SecretStr(SESSION_TOKEN),
                heartbeat=build_heartbeat(),
            )
            claim_clock.now = heartbeat_clock.now + timedelta(seconds=1)
            third = await claimer.claim(
                session_token=SecretStr(SESSION_TOKEN),
                request=ClaimTaskRequest(
                    agent_id=AGENT_ID, claim_request_id=CLAIM_THREE_ID
                ),
            )
            assert third is not None
            assert third.fencing_token == 4
            runtime_clock.now = claim_clock.now + timedelta(seconds=1)
            await runtime.complete(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=third.task_id,
                request=CompleteTaskRequest(
                    **lease_reference(third).model_dump(), outcome="completed"
                ),
            )

            task_row = (
                await connection.execute(
                    select(
                        CollectionTaskRow.status,
                        CollectionTaskRow.page_checkpoint,
                        CollectionTaskRow.processed_count,
                        CollectionTaskRow.leased_agent_id,
                        CollectionTaskRow.leased_profile_id,
                        CollectionTaskRow.lease_token_hash,
                        CollectionTaskRow.lease_expires_at,
                        CollectionTaskRow.fencing_token,
                        CollectionTaskRow.attempt_count,
                    ).where(CollectionTaskRow.id == task_id)
                )
            ).one()
            attempts = (
                await connection.execute(
                    select(
                        CollectionTaskAttemptRow.id,
                        CollectionTaskAttemptRow.outcome,
                        CollectionTaskAttemptRow.failure_code,
                        CollectionTaskAttemptRow.released_at,
                    )
                    .where(CollectionTaskAttemptRow.collection_task_id == task_id)
                    .order_by(CollectionTaskAttemptRow.claimed_at)
                )
            ).all()
            agent_task_id = await connection.scalar(
                select(CollectionAgentRow.current_task_id).where(
                    CollectionAgentRow.id == AGENT_ID
                )
            )
            profile_status = await connection.scalar(
                select(BrowserProfileRow.status).where(
                    BrowserProfileRow.agent_id == AGENT_ID
                )
            )
            job_row = (
                await connection.execute(
                    select(
                        CollectionJobRow.status,
                        CollectionJobRow.completed_at,
                        CollectionJobRow.version,
                    ).where(CollectionJobRow.id == job_id)
                )
            ).one()

            assert task_row.status == "succeeded"
            assert task_row.page_checkpoint == 2
            assert task_row.processed_count == 7
            assert task_row.leased_agent_id is None
            assert task_row.leased_profile_id is None
            assert task_row.lease_token_hash is None
            assert task_row.lease_expires_at is None
            assert task_row.fencing_token == 4
            assert task_row.attempt_count == 3
            assert [row.id for row in attempts] == [
                CLAIM_ONE_ID,
                CLAIM_TWO_ID,
                CLAIM_THREE_ID,
            ]
            assert [row.outcome for row in attempts] == [
                "failed",
                "lease_expired",
                "completed",
            ]
            assert attempts[0].failure_code == "browser_process_exited"
            assert attempts[1].failure_code == "lease_expired"
            assert attempts[2].failure_code is None
            assert all(row.released_at is not None for row in attempts)
            assert agent_task_id is None
            assert profile_status == "ready"
            assert job_row.status == "succeeded"
            assert job_row.completed_at == runtime_clock.now
            assert job_row.version == 7

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert job_id is not None
            assert task_id is not None
            assert (
                await verification_connection.scalar(
                    select(CollectionJobRow.id).where(CollectionJobRow.id == job_id)
                )
                is None
            )
            assert (
                await verification_connection.scalar(
                    select(CollectionTaskRow.id).where(CollectionTaskRow.id == task_id)
                )
                is None
            )
            assert (
                await verification_connection.scalar(
                    select(CollectionAgentRow.id).where(
                        CollectionAgentRow.id == AGENT_ID
                    )
                )
                is None
            )
    finally:
        await engine.dispose()

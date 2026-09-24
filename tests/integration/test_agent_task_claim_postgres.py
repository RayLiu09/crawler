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
from crawler_api.application.agent_task_claims import (
    AgentTaskClaimer,
    AgentTaskClaimUnavailable,
    ClaimRequestConflict,
)
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import CollectionJobSubmitter
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import (
    AgentSessionRow,
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
from crawler_contracts.agent_protocol import AgentHeartbeatRequest, ClaimTaskRequest
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "agent-task-claim-integration-tenant"
TENANT_KEY = "agent-task-claim-integration-key"
ACCESS_TOKEN = "cat_agent_task_claim_integration_token"
AGENT_ONE_ID = UUID("11111111-aaaa-4111-8111-111111111111")
AGENT_TWO_ID = UUID("22222222-bbbb-4222-8222-222222222222")
CLAIM_ONE_ID = UUID("33333333-cccc-4333-8333-333333333333")
CLAIM_TWO_ID = UUID("44444444-dddd-4444-8444-444444444444")
DEVICE_SECRET_ONE = "dev_agent_task_claim_one_secret"
DEVICE_SECRET_TWO = "dev_agent_task_claim_two_secret"
SESSION_TOKEN_ONE = "agt_agent_task_claim_one_session_token"
SESSION_TOKEN_TWO = "agt_agent_task_claim_two_session_token"
BASE_TIME = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
CLAIMED_AT = BASE_TIME + timedelta(minutes=2)


def build_allowlist() -> TenantAllowlist:
    key_hash = hashlib.sha256(TENANT_KEY.encode()).hexdigest()
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Agent task claim integration tenant",
                    "enabled": True,
                    "tenantKeyHashes": [f"sha256:{key_hash}"],
                }
            ],
        }
    )


def build_job_request() -> SubmitCollectionJobRequest:
    return SubmitCollectionJobRequest.model_validate(
        {
            "externalRequestId": "jobreq_agent_claim_integration_001",
            "source": "zhaopin",
            "queries": [
                {"keyword": "Python", "region": "Beijing", "pageLimit": 5},
                {"keyword": "Java", "region": "Shanghai", "pageLimit": 3},
            ],
        }
    )


def build_heartbeat(agent_id: UUID, profile_key: str) -> AgentHeartbeatRequest:
    return AgentHeartbeatRequest.model_validate(
        {
            "agentId": str(agent_id),
            "capabilities": {
                "supportedSources": ["zhaopin"],
                "maxConcurrentTasks": 1,
            },
            "availableSlots": 1,
            "profiles": [
                {
                    "source": "zhaopin",
                    "profileKey": profile_key,
                    "status": "ready",
                }
            ],
        }
    )


def constant_value(value: str) -> Callable[[], str]:
    def generate() -> str:
        return value

    return generate


async def test_two_agents_claim_distinct_tasks_with_replay_and_full_rollback() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    api_token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    device_hasher = DeviceCredentialHasher(settings.agent_device_credential_hash_key)
    session_hasher = AgentSessionTokenHasher(settings.agent_token_hash_key)
    lease_tokens = TaskLeaseTokenManager(settings.agent_token_hash_key)
    job_id = None
    task_ids: list[UUID] = []
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
                idempotency_key=SecretStr("agent-claim-integration-operation"),
                request=build_job_request(),
            )
            job_id = submitted.job.id

            for agent_id, device_secret, session_token, profile_key in (
                (
                    AGENT_ONE_ID,
                    DEVICE_SECRET_ONE,
                    SESSION_TOKEN_ONE,
                    "agent-one-primary",
                ),
                (
                    AGENT_TWO_ID,
                    DEVICE_SECRET_TWO,
                    SESSION_TOKEN_TWO,
                    "agent-two-primary",
                ),
            ):
                await AgentProvisioner(
                    unit_of_work_factory=unit_of_work_factory,
                    hash_device_secret=device_hasher.hash,
                    device_secret_generator=constant_value(device_secret),
                ).provision(
                    display_name=f"Claim integration {agent_id}",
                    agent_id=agent_id,
                )
                await AgentSessionIssuer(
                    unit_of_work_factory=unit_of_work_factory,
                    verify_device_secret=device_hasher.verify,
                    hash_session_token=session_hasher.hash,
                    ttl_seconds=900,
                    clock=lambda: BASE_TIME,
                    token_generator=constant_value(session_token),
                ).issue(agent_id, SecretStr(device_secret))
                await AgentHeartbeatRecorder(
                    unit_of_work_factory=unit_of_work_factory,
                    authenticator=AgentSessionAuthenticator(session_hasher.hash),
                    profile_offline_seconds=180,
                    clock=lambda: BASE_TIME + timedelta(minutes=1),
                ).record(
                    session_token=SecretStr(session_token),
                    heartbeat=build_heartbeat(agent_id, profile_key),
                )

            claimer = AgentTaskClaimer(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                lease_tokens=lease_tokens,
                lease_seconds=120,
                agent_offline_seconds=180,
                clock=lambda: CLAIMED_AT,
            )
            first = await claimer.claim(
                session_token=SecretStr(SESSION_TOKEN_ONE),
                request=ClaimTaskRequest(
                    agent_id=AGENT_ONE_ID,
                    claim_request_id=CLAIM_ONE_ID,
                ),
            )
            assert first is not None
            replay = await claimer.claim(
                session_token=SecretStr(SESSION_TOKEN_ONE),
                request=ClaimTaskRequest(
                    agent_id=AGENT_ONE_ID,
                    claim_request_id=CLAIM_ONE_ID,
                ),
            )
            assert replay == first

            with pytest.raises(AgentTaskClaimUnavailable):
                await claimer.claim(
                    session_token=SecretStr(SESSION_TOKEN_ONE),
                    request=ClaimTaskRequest(
                        agent_id=AGENT_ONE_ID,
                        claim_request_id=CLAIM_TWO_ID,
                    ),
                )
            with pytest.raises(ClaimRequestConflict):
                await claimer.claim(
                    session_token=SecretStr(SESSION_TOKEN_TWO),
                    request=ClaimTaskRequest(
                        agent_id=AGENT_TWO_ID,
                        claim_request_id=CLAIM_ONE_ID,
                    ),
                )

            second = await claimer.claim(
                session_token=SecretStr(SESSION_TOKEN_TWO),
                request=ClaimTaskRequest(
                    agent_id=AGENT_TWO_ID,
                    claim_request_id=CLAIM_TWO_ID,
                ),
            )
            assert second is not None
            assert second.task_id != first.task_id
            assert second.profile_key == "agent-two-primary"
            assert first.profile_key == "agent-one-primary"
            assert first.fencing_token == second.fencing_token == 1
            assert first.lease_expires_at == second.lease_expires_at == (
                CLAIMED_AT + timedelta(seconds=120)
            )

            task_rows = (
                await connection.execute(
                    select(
                        CollectionTaskRow.id,
                        CollectionTaskRow.status,
                        CollectionTaskRow.leased_agent_id,
                        CollectionTaskRow.leased_profile_id,
                        CollectionTaskRow.lease_token_hash,
                        CollectionTaskRow.fencing_token,
                        CollectionTaskRow.attempt_count,
                    )
                    .where(CollectionTaskRow.collection_job_id == job_id)
                    .order_by(CollectionTaskRow.id)
                )
            ).all()
            task_ids = [row.id for row in task_rows]
            attempt_rows = (
                await connection.execute(
                    select(
                        CollectionTaskAttemptRow.id,
                        CollectionTaskAttemptRow.collection_task_id,
                        CollectionTaskAttemptRow.agent_id,
                        CollectionTaskAttemptRow.fencing_token,
                        CollectionTaskAttemptRow.claimed_at,
                        CollectionTaskAttemptRow.released_at,
                        CollectionTaskAttemptRow.outcome,
                    ).order_by(CollectionTaskAttemptRow.id)
                )
            ).all()
            agent_rows = (
                await connection.execute(
                    select(
                        CollectionAgentRow.id,
                        CollectionAgentRow.current_task_id,
                    )
                    .where(CollectionAgentRow.id.in_([AGENT_ONE_ID, AGENT_TWO_ID]))
                    .order_by(CollectionAgentRow.id)
                )
            ).all()
            profile_rows = (
                await connection.execute(
                    select(BrowserProfileRow.id, BrowserProfileRow.status)
                    .where(
                        BrowserProfileRow.agent_id.in_([AGENT_ONE_ID, AGENT_TWO_ID])
                    )
                    .order_by(BrowserProfileRow.id)
                )
            ).all()
            session_last_used = (
                await connection.scalars(
                    select(AgentSessionRow.last_used_at).where(
                        AgentSessionRow.agent_id.in_([AGENT_ONE_ID, AGENT_TWO_ID])
                    )
                )
            ).all()
            job_row = (
                await connection.execute(
                    select(CollectionJobRow.status, CollectionJobRow.version).where(
                        CollectionJobRow.id == job_id
                    )
                )
            ).one()

            assert all(row.status == "running" for row in task_rows)
            assert {row.leased_agent_id for row in task_rows} == {
                AGENT_ONE_ID,
                AGENT_TWO_ID,
            }
            assert len({row.leased_profile_id for row in task_rows}) == 2
            assert all(row.lease_token_hash.startswith("hmac-sha256:") for row in task_rows)
            assert all(row.fencing_token == 1 for row in task_rows)
            assert all(row.attempt_count == 1 for row in task_rows)
            assert {row.id for row in attempt_rows} == {CLAIM_ONE_ID, CLAIM_TWO_ID}
            assert {row.collection_task_id for row in attempt_rows} == set(task_ids)
            assert all(row.fencing_token == 1 for row in attempt_rows)
            assert all(row.claimed_at == CLAIMED_AT for row in attempt_rows)
            assert all(row.released_at is None for row in attempt_rows)
            assert all(row.outcome is None for row in attempt_rows)
            assert {row.current_task_id for row in agent_rows} == set(task_ids)
            assert all(row.status == "busy" for row in profile_rows)
            assert all(value == CLAIMED_AT for value in session_last_used)
            assert job_row.status == "running"
            assert job_row.version == 2
            assert await connection.scalar(
                select(func.count()).select_from(CollectionTaskAttemptRow)
            ) == 2

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert job_id is not None
            assert (
                await verification_connection.scalar(
                    select(CollectionJobRow.id).where(CollectionJobRow.id == job_id)
                )
                is None
            )
            assert not (
                await verification_connection.scalars(
                    select(CollectionAgentRow.id).where(
                        CollectionAgentRow.id.in_([AGENT_ONE_ID, AGENT_TWO_ID])
                    )
                )
            ).all()
            assert not (
                await verification_connection.scalars(
                    select(CollectionTaskRow.id).where(CollectionTaskRow.id.in_(task_ids))
                )
            ).all()
    finally:
        await engine.dispose()

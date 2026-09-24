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
from crawler_api.application.agent_task_runtime import AgentTaskRuntime
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import (
    CollectionJobNotFound,
    CollectionJobSubmitter,
)
from crawler_api.application.parsed_records import (
    ParsedRecordBatchConflict,
    ParsedRecordBatchIngestor,
    ParsedRecordReader,
)
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
    ParsedJobRecordRow,
    ParsedRecordBatchRow,
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
    ParsedRecordBatchRequest,
    TaskLease,
)
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from crawler_contracts.parsed_jobs import (
    ParsedJobFields,
    ParsedJobRecord,
    calculate_payload_hash,
)
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "parsed-records-integration-tenant"
TENANT_KEY = "parsed-records-integration-key"
OTHER_TENANT_ID = "parsed-records-other-tenant"
OTHER_TENANT_KEY = "parsed-records-other-key"
ACCESS_TOKEN = "cat_parsed_records_integration_token"
OTHER_ACCESS_TOKEN = "cat_parsed_records_other_token"
AGENT_ID = UUID("12121212-aaaa-4121-8121-121212121212")
CLAIM_ID = UUID("34343434-bbbb-4343-8343-343434343434")
BATCH_ONE_ID = UUID("56565656-cccc-4565-8565-565656565656")
BATCH_TWO_ID = UUID("78787878-dddd-4787-8787-787878787878")
DEVICE_SECRET = "dev_parsed_records_integration_secret"
SESSION_TOKEN = "agt_parsed_records_integration_session"
BASE_TIME = datetime(2026, 9, 17, 17, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def constant_value(value: str) -> Callable[[], str]:
    return lambda: value


def tenant_entry(tenant_id: str, display_name: str, tenant_key: str) -> dict[str, object]:
    key_hash = hashlib.sha256(tenant_key.encode()).hexdigest()
    return {
        "tenantId": tenant_id,
        "displayName": display_name,
        "enabled": True,
        "tenantKeyHashes": [f"sha256:{key_hash}"],
    }


def build_allowlist() -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                tenant_entry(TENANT_ID, "Parsed records tenant", TENANT_KEY),
                tenant_entry(
                    OTHER_TENANT_ID, "Parsed records other tenant", OTHER_TENANT_KEY
                ),
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
                    "profileKey": "records-primary",
                    "status": "ready",
                }
            ],
        }
    )


def record(title: str, *, source_url: str | None = None) -> ParsedJobRecord:
    parsed_fields = ParsedJobFields(
        title=title,
        company_name="Integration Co",
        salary_text="20k-30k",
    )
    return ParsedJobRecord(
        source="zhaopin",
        source_job_id=f"source-{title}",
        source_url=source_url or f"https://jobs.example/{title}",
        keyword="Python",
        region="Shanghai",
        page=1,
        fields=parsed_fields,
        collected_at=BASE_TIME + timedelta(minutes=2),
        payload_hash=calculate_payload_hash(parsed_fields),
    )


def batch_request(
    lease: TaskLease,
    batch_id: UUID,
    records: list[ParsedJobRecord],
) -> ParsedRecordBatchRequest:
    return ParsedRecordBatchRequest(
        agent_id=lease.agent_id,
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        fencing_token=lease.fencing_token,
        lease_expires_at=lease.lease_expires_at,
        batch_id=batch_id,
        records=records,
    )


async def test_batch_ingestion_pagination_tenant_boundary_and_rollback() -> None:
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
            issuer = AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=api_token_hasher.hash,
                ttl_seconds=3600,
                clock=lambda: BASE_TIME,
                token_generator=constant_value(ACCESS_TOKEN),
            )
            issued = await issuer.issue(SecretStr(TENANT_KEY))
            other_issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=api_token_hasher.hash,
                ttl_seconds=3600,
                clock=lambda: BASE_TIME,
                token_generator=constant_value(OTHER_ACCESS_TOKEN),
            ).issue(SecretStr(OTHER_TENANT_KEY))
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
                idempotency_key=SecretStr("parsed-records-integration-operation"),
                request=SubmitCollectionJobRequest.model_validate(
                    {
                        "externalRequestId": "jobreq_parsed_records_integration",
                        "source": "zhaopin",
                        "queries": [
                            {
                                "keyword": "Python",
                                "region": "Shanghai",
                                "pageLimit": 3,
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
            ).provision(display_name="Parsed records Agent", agent_id=AGENT_ID)
            await AgentSessionIssuer(
                unit_of_work_factory=unit_of_work_factory,
                verify_device_secret=device_hasher.verify,
                hash_session_token=session_hasher.hash,
                ttl_seconds=900,
                clock=lambda: BASE_TIME,
                token_generator=constant_value(SESSION_TOKEN),
            ).issue(AGENT_ID, SecretStr(DEVICE_SECRET))
            await AgentHeartbeatRecorder(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                profile_offline_seconds=180,
                clock=lambda: BASE_TIME + timedelta(minutes=1),
            ).record(
                session_token=SecretStr(SESSION_TOKEN),
                heartbeat=build_heartbeat(),
            )
            lease = await AgentTaskClaimer(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                lease_tokens=lease_tokens,
                lease_seconds=120,
                agent_offline_seconds=180,
                clock=lambda: BASE_TIME + timedelta(minutes=2),
            ).claim(
                session_token=SecretStr(SESSION_TOKEN),
                request=ClaimTaskRequest(
                    agent_id=AGENT_ID,
                    claim_request_id=CLAIM_ID,
                ),
            )
            assert lease is not None
            task_id = lease.task_id
            lease_authenticator = ActiveTaskLeaseAuthenticator(
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                lease_tokens=lease_tokens,
            )
            ingest_clock = MutableClock(BASE_TIME + timedelta(minutes=2, seconds=10))
            ingestor = ParsedRecordBatchIngestor(
                unit_of_work_factory=unit_of_work_factory,
                lease_authenticator=lease_authenticator,
                clock=ingest_clock,
            )

            first_request = batch_request(
                lease,
                BATCH_ONE_ID,
                [record("Engineer"), record("Architect")],
            )
            first = await ingestor.ingest(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=lease.task_id,
                request=first_request,
            )
            replay = await ingestor.ingest(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=lease.task_id,
                request=first_request,
            )
            assert replay == first
            assert first.inserted_count == 2
            assert first.duplicate_count == 0

            with pytest.raises(ParsedRecordBatchConflict):
                await ingestor.ingest(
                    session_token=SecretStr(SESSION_TOKEN),
                    task_id=lease.task_id,
                    request=batch_request(
                        lease, BATCH_ONE_ID, [record("Changed")]
                    ),
                )

            ingest_clock.now += timedelta(seconds=1)
            second = await ingestor.ingest(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=lease.task_id,
                request=batch_request(
                    lease,
                    BATCH_TWO_ID,
                    [record("Engineer"), record("Manager")],
                ),
            )
            assert second.inserted_count == 1
            assert second.duplicate_count == 1
            assert second.accepted_record_ids[0] == first.accepted_record_ids[0]

            reader = ParsedRecordReader(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=CallerAccessTokenAuthenticator(api_token_hasher.hash),
                clock=lambda: ingest_clock.now,
            )
            first_page = await reader.list_for_job(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                cursor=None,
                limit=2,
            )
            assert len(first_page.items) == 2
            assert first_page.next_cursor is not None
            second_page = await reader.list_for_job(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                cursor=first_page.next_cursor,
                limit=2,
            )
            assert len(second_page.items) == 1
            assert second_page.next_cursor is None
            assert {item.record_id for item in first_page.items + second_page.items} == set(
                first.accepted_record_ids + second.accepted_record_ids
            )
            with pytest.raises(CollectionJobNotFound):
                await reader.list_for_job(
                    access_token=SecretStr(other_issued.access_token),
                    job_id=job_id,
                    cursor=None,
                    limit=10,
                )

            complete_clock = ingest_clock.now + timedelta(seconds=1)
            await AgentTaskRuntime(
                unit_of_work_factory=unit_of_work_factory,
                lease_authenticator=lease_authenticator,
                lease_seconds=120,
                clock=lambda: complete_clock,
            ).complete(
                session_token=SecretStr(SESSION_TOKEN),
                task_id=lease.task_id,
                request=CompleteTaskRequest(
                    agent_id=lease.agent_id,
                    task_id=lease.task_id,
                    lease_token=lease.lease_token,
                    fencing_token=lease.fencing_token,
                    lease_expires_at=lease.lease_expires_at,
                    outcome="completed",
                ),
            )
            with pytest.raises(TaskLeaseStateConflict):
                await ingestor.ingest(
                    session_token=SecretStr(SESSION_TOKEN),
                    task_id=lease.task_id,
                    request=batch_request(lease, UUID(int=9), [record("Late")]),
                )

            assert await connection.scalar(
                select(func.count()).select_from(ParsedJobRecordRow).where(
                    ParsedJobRecordRow.collection_task_id == task_id
                )
            ) == 3
            receipts = (
                await connection.execute(
                    select(
                        ParsedRecordBatchRow.id,
                        ParsedRecordBatchRow.collection_task_id,
                        ParsedRecordBatchRow.attempt_id,
                        ParsedRecordBatchRow.record_ids,
                        ParsedRecordBatchRow.inserted_count,
                    )
                    .where(ParsedRecordBatchRow.collection_task_id == task_id)
                    .order_by(ParsedRecordBatchRow.created_at)
                )
            ).all()
            assert [row.id for row in receipts] == [BATCH_ONE_ID, BATCH_TWO_ID]
            assert all(row.collection_task_id == task_id for row in receipts)
            assert all(row.attempt_id == CLAIM_ID for row in receipts)
            assert [row.inserted_count for row in receipts] == [2, 1]
            assert [len(row.record_ids) for row in receipts] == [2, 2]

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert job_id is not None
            assert task_id is not None
            assert (
                await verification_connection.scalar(
                    select(ParsedJobRecordRow.id).where(
                        ParsedJobRecordRow.collection_task_id == task_id
                    )
                )
                is None
            )
            assert (
                await verification_connection.scalar(
                    select(ParsedRecordBatchRow.id).where(
                        ParsedRecordBatchRow.collection_task_id == task_id
                    )
                )
                is None
            )
    finally:
        await engine.dispose()

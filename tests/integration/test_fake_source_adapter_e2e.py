import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
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
from crawler_api.application.collection_jobs import CollectionJobReader, CollectionJobSubmitter
from crawler_api.application.parsed_records import ParsedRecordBatchIngestor, ParsedRecordReader
from crawler_api.application.task_leases import ActiveTaskLeaseAuthenticator
from crawler_api.application.tenant_allowlist import TenantAllowlist, TenantAllowlistSynchronizer
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import Base
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from crawler_api.infrastructure.security.task_lease_tokens import TaskLeaseTokenManager
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_api.main import create_app
from desktop_agent.source_adapters import FakeSourceAdapter
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "fake-adapter-e2e-tenant"
TENANT_KEY = "fake-adapter-e2e-tenant-key"
ACCESS_TOKEN = "cat_fake_adapter_e2e_access_token_0001"
AGENT_ID = UUID("10000000-0000-4000-8000-000000000001")
DEVICE_SECRET = "dev_fake_adapter_e2e_device_secret_0001"
SESSION_TOKEN = "agt_fake_adapter_e2e_session_token_0001"
CLAIM_REQUEST_ID = UUID("20000000-0000-4000-8000-000000000001")
BATCH_IDS = (
    UUID("30000000-0000-4000-8000-000000000001"),
    UUID("30000000-0000-4000-8000-000000000002"),
)
BASE_TIME = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingControl:
    def __init__(self) -> None:
        self.control_checks = 0
        self.checkpoints: list[tuple[int, int]] = []

    def raise_if_control_requested(self) -> None:
        self.control_checks += 1

    def checkpoint(self, page: int, processed_count: int) -> None:
        self.checkpoints.append((page, processed_count))


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
                    "displayName": "Fake Adapter E2E tenant",
                    "enabled": True,
                    "tenantKeyHashes": [f"sha256:{key_hash}"],
                }
            ],
        }
    )


async def table_counts(connection: AsyncConnection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in Base.metadata.tables.values():
        counts[table.fullname] = int(
            (await connection.execute(select(func.count()).select_from(table))).scalar_one()
        )
    return counts


def assert_agent_response_is_caller_neutral(response: Response) -> None:
    response_text = response.text
    for forbidden_name in ("tenantId", "clientId", "callback", "metadata"):
        assert forbidden_name not in response_text


async def test_fake_adapter_completes_real_api_agent_http_flow() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    operation_clock = MutableClock(BASE_TIME)
    allowlist = build_allowlist()
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    device_hasher = DeviceCredentialHasher(settings.agent_device_credential_hash_key)
    session_hasher = AgentSessionTokenHasher(settings.agent_token_hash_key)
    lease_tokens = TaskLeaseTokenManager(settings.agent_token_hash_key)

    try:
        async with engine.connect() as baseline_connection:
            baseline_counts = await table_counts(baseline_connection)

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

            await TenantAllowlistSynchronizer(unit_of_work_factory).synchronize(allowlist)
            await AgentProvisioner(
                unit_of_work_factory=unit_of_work_factory,
                hash_device_secret=device_hasher.hash,
                device_secret_generator=constant_value(DEVICE_SECRET),
            ).provision(display_name="Fake Adapter E2E Agent", agent_id=AGENT_ID)

            caller_authenticator = CallerAccessTokenAuthenticator(token_hasher.hash)
            agent_authenticator = AgentSessionAuthenticator(session_hasher.hash)
            lease_authenticator = ActiveTaskLeaseAuthenticator(
                authenticator=agent_authenticator,
                lease_tokens=lease_tokens,
            )
            app = create_app()
            app.state.access_token_issuer = AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=3600,
                clock=operation_clock,
                token_generator=constant_value(ACCESS_TOKEN),
            )
            app.state.collection_job_submitter = CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=caller_authenticator,
                tenant_allowlist=allowlist,
                callback_token_protector=CallbackTokenProtector(
                    encryption_key=settings.callback_token_encryption_key,
                    fingerprint_key=settings.callback_token_fingerprint_key,
                    encryption_key_version=settings.callback_token_encryption_key_version,
                ),
                clock=operation_clock,
            )
            app.state.collection_job_reader = CollectionJobReader(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=caller_authenticator,
                clock=operation_clock,
            )
            app.state.parsed_record_reader = ParsedRecordReader(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=caller_authenticator,
                clock=operation_clock,
            )
            app.state.agent_session_issuer = AgentSessionIssuer(
                unit_of_work_factory=unit_of_work_factory,
                verify_device_secret=device_hasher.verify,
                hash_session_token=session_hasher.hash,
                ttl_seconds=1800,
                clock=operation_clock,
                token_generator=constant_value(SESSION_TOKEN),
            )
            app.state.agent_heartbeat_recorder = AgentHeartbeatRecorder(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=agent_authenticator,
                profile_offline_seconds=180,
                clock=operation_clock,
            )
            app.state.agent_task_claimer = AgentTaskClaimer(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=agent_authenticator,
                lease_tokens=lease_tokens,
                lease_seconds=120,
                agent_offline_seconds=180,
                clock=operation_clock,
            )
            app.state.agent_task_runtime = AgentTaskRuntime(
                unit_of_work_factory=unit_of_work_factory,
                lease_authenticator=lease_authenticator,
                lease_seconds=120,
                clock=operation_clock,
            )
            app.state.parsed_record_batch_ingestor = ParsedRecordBatchIngestor(
                unit_of_work_factory=unit_of_work_factory,
                lease_authenticator=lease_authenticator,
                clock=operation_clock,
            )

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                token_response = await client.post(
                    "/v1/auth/token", json={"tenantKey": TENANT_KEY}
                )
                assert token_response.status_code == 200
                assert token_response.json() == {
                    "accessToken": ACCESS_TOKEN,
                    "tokenType": "Bearer",
                    "expiresIn": 3600,
                    "scopes": [
                        "collection:write",
                        "collection:read",
                        "collection:control",
                    ],
                }
                caller_headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

                operation_clock.now += timedelta(seconds=1)
                submit_response = await client.post(
                    "/v1/collection-jobs",
                    headers={
                        **caller_headers,
                        "Idempotency-Key": "fake-adapter-e2e-submit-001",
                    },
                    json={
                        "externalRequestId": "jobreq_fake_adapter_e2e_001",
                        "source": "zhaopin",
                        "queries": [
                            {
                                "keyword": "Python",
                                "region": "Shanghai",
                                "pageLimit": 2,
                            }
                        ],
                    },
                )
                assert submit_response.status_code == 202
                job_id = UUID(submit_response.json()["collectionJobId"])

                operation_clock.now += timedelta(seconds=1)
                session_response = await client.post(
                    "/v1/agent/sessions",
                    json={"agentId": str(AGENT_ID), "deviceSecret": DEVICE_SECRET},
                )
                assert session_response.status_code == 200
                assert session_response.json()["sessionToken"] == SESSION_TOKEN
                assert_agent_response_is_caller_neutral(session_response)
                agent_headers = {"Authorization": f"Bearer {SESSION_TOKEN}"}

                operation_clock.now += timedelta(seconds=1)
                heartbeat_response = await client.post(
                    "/v1/agent/heartbeat",
                    headers=agent_headers,
                    json={
                        "agentId": str(AGENT_ID),
                        "capabilities": {
                            "schemaVersion": 1,
                            "supportedSources": ["zhaopin"],
                            "maxConcurrentTasks": 1,
                            "browser": {"name": "chrome", "headful": True},
                        },
                        "availableSlots": 1,
                        "profiles": [
                            {
                                "source": "zhaopin",
                                "profileKey": "fake-primary",
                                "status": "ready",
                            }
                        ],
                    },
                )
                assert heartbeat_response.status_code == 204

                operation_clock.now += timedelta(seconds=1)
                claim_response = await client.post(
                    "/v1/agent/tasks/claim",
                    headers=agent_headers,
                    json={
                        "agentId": str(AGENT_ID),
                        "claimRequestId": str(CLAIM_REQUEST_ID),
                    },
                )
                assert claim_response.status_code == 200
                lease = claim_response.json()
                assert lease["source"] == "zhaopin"
                assert lease["keyword"] == "Python"
                assert lease["region"] == "Shanghai"
                assert lease["pageLimit"] == 2
                assert_agent_response_is_caller_neutral(claim_response)

                control = RecordingControl()
                adapter = FakeSourceAdapter(
                    records_per_page=2,
                    clock=lambda: BASE_TIME + timedelta(minutes=1),
                )
                records = list(
                    adapter.collect(
                        lease["keyword"],
                        lease["region"],
                        lease["pageLimit"],
                        control,
                    )
                )
                assert control.control_checks == 6
                assert control.checkpoints == [(1, 2), (2, 4)]

                task_id = lease["taskId"]
                processed_count = 0
                for page, batch_id in enumerate(BATCH_IDS, start=1):
                    page_records = [record for record in records if record.page == page]
                    operation_clock.now += timedelta(seconds=1)
                    batch_payload: dict[str, Any] = {
                        "agentId": str(AGENT_ID),
                        "taskId": task_id,
                        "leaseToken": lease["leaseToken"],
                        "fencingToken": lease["fencingToken"],
                        "leaseExpiresAt": lease["leaseExpiresAt"],
                        "batchId": str(batch_id),
                        "records": [
                            record.model_dump(mode="json", by_alias=True)
                            for record in page_records
                        ],
                    }
                    batch_response = await client.post(
                        f"/v1/agent/tasks/{task_id}/records",
                        headers=agent_headers,
                        json=batch_payload,
                    )
                    assert batch_response.status_code == 200
                    assert batch_response.json()["insertedCount"] == 2
                    assert batch_response.json()["duplicateCount"] == 0
                    assert_agent_response_is_caller_neutral(batch_response)

                    if page == 1:
                        replay_response = await client.post(
                            f"/v1/agent/tasks/{task_id}/records",
                            headers=agent_headers,
                            json=batch_payload,
                        )
                        assert replay_response.status_code == 200
                        assert replay_response.json() == batch_response.json()

                    processed_count += len(page_records)
                    operation_clock.now += timedelta(seconds=1)
                    checkpoint_response = await client.post(
                        f"/v1/agent/tasks/{task_id}/checkpoint",
                        headers=agent_headers,
                        json={
                            "agentId": str(AGENT_ID),
                            "taskId": task_id,
                            "leaseToken": lease["leaseToken"],
                            "fencingToken": lease["fencingToken"],
                            "leaseExpiresAt": lease["leaseExpiresAt"],
                            "pageCheckpoint": page,
                            "processedCount": processed_count,
                        },
                    )
                    assert checkpoint_response.status_code == 200
                    assert checkpoint_response.json()["desiredState"] == "running"
                    assert_agent_response_is_caller_neutral(checkpoint_response)
                    lease["leaseExpiresAt"] = checkpoint_response.json()["leaseExpiresAt"]

                operation_clock.now += timedelta(seconds=1)
                complete_response = await client.post(
                    f"/v1/agent/tasks/{task_id}/complete",
                    headers=agent_headers,
                    json={
                        "agentId": str(AGENT_ID),
                        "taskId": task_id,
                        "leaseToken": lease["leaseToken"],
                        "fencingToken": lease["fencingToken"],
                        "leaseExpiresAt": lease["leaseExpiresAt"],
                        "outcome": "completed",
                    },
                )
                assert complete_response.status_code == 204

                operation_clock.now += timedelta(seconds=1)
                job_response = await client.get(
                    f"/v1/collection-jobs/{job_id}", headers=caller_headers
                )
                assert job_response.status_code == 200
                assert job_response.json()["status"] == "succeeded"
                assert job_response.json()["processedCount"] == 4
                assert job_response.json()["taskStatusCounts"]["succeeded"] == 1

                returned_records: list[dict[str, Any]] = []
                cursor: str | None = None
                while True:
                    params = {"limit": "2"}
                    if cursor is not None:
                        params["cursor"] = cursor
                    records_response = await client.get(
                        f"/v1/collection-jobs/{job_id}/records",
                        headers=caller_headers,
                        params=params,
                    )
                    assert records_response.status_code == 200
                    page_body = records_response.json()
                    returned_records.extend(page_body["items"])
                    cursor = page_body["nextCursor"]
                    if cursor is None:
                        break

                expected_by_source_id = {
                    record.source_job_id: record.model_dump(mode="json", by_alias=True)
                    for record in records
                }
                assert len(returned_records) == 4
                for returned in returned_records:
                    expected = expected_by_source_id[returned["sourceJobId"]]
                    assert {
                        key: returned[key]
                        for key in expected
                    } == expected
                    assert returned["taskId"] == task_id

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert await table_counts(verification_connection) == baseline_counts
    finally:
        await engine.dispose()

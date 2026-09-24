import hashlib
import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import (
    CollectionJobControlAction,
    CollectionJobControlConflict,
    CollectionJobController,
    CollectionJobNotFound,
    CollectionJobReader,
    CollectionJobSubmitter,
)
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.domain.enums import CollectionState, DesiredState
from crawler_api.infrastructure.database.models import (
    ApiClientRow,
    CollectionJobRow,
    CollectionTaskRow,
)
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "collection-job-control-integration-tenant"
TENANT_KEY = "collection-job-control-integration-key"
OTHER_TENANT_ID = "collection-job-control-other-tenant"
OTHER_TENANT_KEY = "collection-job-control-other-key"
ACCESS_TOKEN = "cat_collection_job_control_integration_token"
OTHER_ACCESS_TOKEN = "cat_collection_job_control_other_token"
ISSUED_AT = datetime(2026, 9, 17, 8, 59, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
CONTROLLED_AT = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 9, 17, 10, 30, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def tenant_entry(tenant_id: str, tenant_key: str) -> dict[str, object]:
    key_hash = hashlib.sha256(tenant_key.encode()).hexdigest()
    return {
        "tenantId": tenant_id,
        "displayName": tenant_id,
        "enabled": True,
        "tenantKeyHashes": [f"sha256:{key_hash}"],
    }


def build_allowlist() -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                tenant_entry(TENANT_ID, TENANT_KEY),
                tenant_entry(OTHER_TENANT_ID, OTHER_TENANT_KEY),
            ],
        }
    )


def build_request() -> SubmitCollectionJobRequest:
    return SubmitCollectionJobRequest.model_validate(
        {
            "externalRequestId": "jobreq_control_integration_001",
            "source": "zhaopin",
            "queries": [
                {"keyword": "Python", "region": "Beijing", "pageLimit": 5},
                {"keyword": "Java", "region": "Shanghai", "pageLimit": 3},
            ],
        }
    )


async def test_collection_job_query_and_control_are_atomic_and_rolled_back() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    client_ids: list[UUID] = []
    job_id = None
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
                hash_token=token_hasher.hash,
                ttl_seconds=7200,
                clock=lambda: ISSUED_AT,
                token_generator=lambda: ACCESS_TOKEN,
            )
            issued = await issuer.issue(SecretStr(TENANT_KEY))
            other_issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=7200,
                clock=lambda: ISSUED_AT,
                token_generator=lambda: OTHER_ACCESS_TOKEN,
            ).issue(SecretStr(OTHER_TENANT_KEY))

            authenticator = CallerAccessTokenAuthenticator(token_hasher.hash)
            submitted = await CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                tenant_allowlist=allowlist,
                callback_token_protector=CallbackTokenProtector(
                    encryption_key=settings.callback_token_encryption_key,
                    fingerprint_key=settings.callback_token_fingerprint_key,
                    encryption_key_version=settings.callback_token_encryption_key_version,
                ),
                clock=lambda: SUBMITTED_AT,
            ).submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("control-integration-operation"),
                request=build_request(),
            )
            job_id = submitted.job.id
            clock = MutableClock(CONTROLLED_AT)
            reader = CollectionJobReader(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                clock=clock,
            )
            controller = CollectionJobController(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                clock=clock,
            )

            initial = await reader.get(
                access_token=SecretStr(issued.access_token), job_id=job_id
            )
            assert initial.task_status_counts == {CollectionState.QUEUED: 2}
            assert initial.job.version == 1

            paused = await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.PAUSE,
            )
            paused_replay = await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.PAUSE,
            )
            assert paused.job.status is CollectionState.PAUSED
            assert paused.job.desired_state is DesiredState.PAUSED
            assert paused.task_status_counts == {CollectionState.PAUSED: 2}
            assert paused_replay.job.version == 2

            first_task_id = await connection.scalar(
                select(CollectionTaskRow.id)
                .where(CollectionTaskRow.collection_job_id == job_id)
                .order_by(CollectionTaskRow.id)
                .limit(1)
            )
            assert first_task_id is not None
            await connection.execute(
                update(CollectionTaskRow)
                .where(CollectionTaskRow.id == first_task_id)
                .values(page_checkpoint=4)
            )

            resumed = await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.RESUME,
            )
            assert resumed.job.status is CollectionState.QUEUED
            assert resumed.job.version == 3
            assert resumed.task_status_counts == {CollectionState.QUEUED: 2}
            assert (
                await connection.scalar(
                    select(CollectionTaskRow.page_checkpoint).where(
                        CollectionTaskRow.id == first_task_id
                    )
                )
                == 4
            )

            await connection.execute(
                update(CollectionTaskRow)
                .where(CollectionTaskRow.id == first_task_id)
                .values(status=CollectionState.RUNNING.value)
            )
            await connection.execute(
                update(CollectionJobRow)
                .where(CollectionJobRow.id == job_id)
                .values(status=CollectionState.RUNNING.value)
            )
            pausing = await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.PAUSE,
            )
            assert pausing.job.status is CollectionState.PAUSING
            assert pausing.job.version == 4
            assert pausing.task_status_counts == {
                CollectionState.PAUSING: 1,
                CollectionState.PAUSED: 1,
            }
            with pytest.raises(CollectionJobControlConflict):
                await controller.control(
                    access_token=SecretStr(issued.access_token),
                    job_id=job_id,
                    action=CollectionJobControlAction.RESUME,
                )

            await connection.execute(
                update(CollectionTaskRow)
                .where(CollectionTaskRow.id == first_task_id)
                .values(status=CollectionState.PAUSED.value)
            )
            await connection.execute(
                update(CollectionJobRow)
                .where(CollectionJobRow.id == job_id)
                .values(status=CollectionState.PAUSED.value)
            )
            resumed_again = await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.RESUME,
            )
            assert resumed_again.job.version == 5
            assert resumed_again.task_status_counts == {CollectionState.QUEUED: 2}

            await connection.execute(
                update(CollectionTaskRow)
                .where(CollectionTaskRow.id == first_task_id)
                .values(
                    status=CollectionState.RUNNING.value,
                    processed_count=3,
                )
            )
            await connection.execute(
                update(CollectionJobRow)
                .where(CollectionJobRow.id == job_id)
                .values(status=CollectionState.RUNNING.value)
            )
            cancelling = await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.CANCEL,
            )
            cancelling_replay = await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.CANCEL,
            )
            assert cancelling.job.status is CollectionState.CANCELLING
            assert cancelling.job.completed_at is None
            assert cancelling.job.version == 6
            assert cancelling.task_status_counts == {
                CollectionState.CANCELLING: 1,
                CollectionState.CANCELLED: 1,
            }
            assert cancelling_replay.job.version == 6

            await connection.execute(
                update(CollectionTaskRow)
                .where(CollectionTaskRow.id == first_task_id)
                .values(status=CollectionState.CANCELLED.value)
            )
            await connection.execute(
                update(CollectionJobRow)
                .where(CollectionJobRow.id == job_id)
                .values(
                    status=CollectionState.CANCELLED.value,
                    completed_at=COMPLETED_AT,
                )
            )
            final = await reader.get(
                access_token=SecretStr(issued.access_token), job_id=job_id
            )
            assert final.job.status is CollectionState.CANCELLED
            assert final.task_status_counts == {CollectionState.CANCELLED: 2}
            assert final.processed_count == 3
            assert final.job.completed_at == COMPLETED_AT

            with pytest.raises(CollectionJobNotFound):
                await reader.get(
                    access_token=SecretStr(other_issued.access_token), job_id=job_id
                )

            client_ids = list(
                (
                    await connection.scalars(
                        select(ApiClientRow.id).where(
                            ApiClientRow.tenant_id.in_([TENANT_ID, OTHER_TENANT_ID])
                        )
                    )
                ).all()
            )
            assert len(client_ids) == 2
            assert await connection.scalar(
                select(func.count()).select_from(CollectionJobRow)
            ) == 1
            assert await connection.scalar(
                select(func.count()).select_from(CollectionTaskRow)
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
                    select(ApiClientRow.id).where(ApiClientRow.id.in_(client_ids))
                )
            ).all()
    finally:
        await engine.dispose()

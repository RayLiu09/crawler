import hashlib
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.caller_authentication import (
    CallerAccessTokenAuthenticationDenied,
    CallerAccessTokenAuthenticator,
)
from crawler_api.application.collection_jobs import (
    CollectionJobIdempotencyConflict,
    CollectionJobSubmitter,
    hash_idempotency_key,
)
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.domain.entities import CollectionJob
from crawler_api.infrastructure.database.models import (
    ApiAccessTokenRow,
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "collection-job-submission-integration-tenant"
TENANT_KEY = "collection-job-submission-integration-key"
ACCESS_TOKEN = "cat_collection_job_integration_access_token"
IDEMPOTENCY_KEY = "collection-job-integration-operation-001"
ISSUED_AT = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 9, 17, 9, 1, tzinfo=UTC)


def build_allowlist(*, enabled: bool = True) -> TenantAllowlist:
    key_hash = hashlib.sha256(TENANT_KEY.encode()).hexdigest()
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Collection job integration tenant",
                    "enabled": enabled,
                    "tenantKeyHashes": [f"sha256:{key_hash}"],
                    "scopes": ["collection:write"],
                }
            ],
        }
    )


def build_request(
    *,
    external_request_id: str = "jobreq_integration_001",
    keyword: str = "Python",
) -> SubmitCollectionJobRequest:
    return SubmitCollectionJobRequest.model_validate(
        {
            "externalRequestId": external_request_id,
            "source": "zhaopin",
            "queries": [
                {"keyword": keyword, "region": "Beijing", "pageLimit": 2},
                {"keyword": "Java", "region": "Shanghai", "pageLimit": 3},
            ],
            "collectionMode": "full",
            "metadata": {"trace": "integration"},
        }
    )


async def test_collection_job_submission_is_idempotent_and_rolled_back() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    client_id = None
    token_id = None
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

            synchronizer = TenantAllowlistSynchronizer(unit_of_work_factory)
            allowlist = build_allowlist()
            await synchronizer.synchronize(allowlist)
            issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=3600,
                clock=lambda: ISSUED_AT,
                token_generator=lambda: ACCESS_TOKEN,
            ).issue(SecretStr(TENANT_KEY))
            submitter = CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=CallerAccessTokenAuthenticator(token_hasher.hash),
                tenant_allowlist=allowlist,
                callback_token_protector=CallbackTokenProtector(
                    encryption_key=settings.callback_token_encryption_key,
                    fingerprint_key=settings.callback_token_fingerprint_key,
                    encryption_key_version=settings.callback_token_encryption_key_version,
                ),
                clock=lambda: SUBMITTED_AT,
            )
            request = build_request()

            first = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr(IDEMPOTENCY_KEY),
                request=request,
            )
            replay_by_key = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr(IDEMPOTENCY_KEY),
                request=request,
            )
            replay_by_external_request = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("different-operation-key"),
                request=request,
            )

            assert first.created
            assert first.task_count == 2
            assert not replay_by_key.created
            assert not replay_by_external_request.created
            assert replay_by_key.job.id == first.job.id
            assert replay_by_external_request.job.id == first.job.id

            with pytest.raises(CollectionJobIdempotencyConflict):
                await submitter.submit(
                    access_token=SecretStr(issued.access_token),
                    idempotency_key=SecretStr(IDEMPOTENCY_KEY),
                    request=build_request(external_request_id="jobreq_integration_002"),
                )
            with pytest.raises(CollectionJobIdempotencyConflict):
                await submitter.submit(
                    access_token=SecretStr(issued.access_token),
                    idempotency_key=SecretStr("third-operation-key"),
                    request=build_request(keyword="Go"),
                )

            client_id = await connection.scalar(
                select(ApiClientRow.id).where(ApiClientRow.tenant_id == TENANT_ID)
            )
            assert client_id is not None
            token_row = (
                await connection.execute(
                    select(ApiAccessTokenRow.id, ApiAccessTokenRow.last_used_at).where(
                        ApiAccessTokenRow.client_id == client_id
                    )
                )
            ).one()
            token_id = token_row.id
            job_row = (
                await connection.execute(
                    select(
                        CollectionJobRow.id,
                        CollectionJobRow.idempotency_key_hash,
                        CollectionJobRow.external_request_id,
                        CollectionJobRow.request_payload,
                        CollectionJobRow.status,
                        CollectionJobRow.desired_state,
                    ).where(CollectionJobRow.client_id == client_id)
                )
            ).one()
            job_id = job_row.id
            task_rows = (
                await connection.execute(
                    select(
                        CollectionTaskRow.keyword,
                        CollectionTaskRow.region,
                        CollectionTaskRow.page_limit,
                        CollectionTaskRow.status,
                        CollectionTaskRow.desired_state,
                    )
                    .where(CollectionTaskRow.collection_job_id == job_id)
                    .order_by(CollectionTaskRow.id)
                )
            ).all()

            assert token_row.last_used_at == SUBMITTED_AT
            assert job_row.idempotency_key_hash == hash_idempotency_key(
                SecretStr(IDEMPOTENCY_KEY)
            )
            assert IDEMPOTENCY_KEY not in job_row.idempotency_key_hash
            assert job_row.external_request_id == request.external_request_id
            assert job_row.request_payload == request.canonical_payload().model_dump(
                mode="json", by_alias=True
            )
            assert "externalRequestId" not in job_row.request_payload
            assert job_row.status == "queued"
            assert job_row.desired_state == "running"
            assert {(row.keyword, row.region, row.page_limit) for row in task_rows} == {
                ("Python", "Beijing", 2),
                ("Java", "Shanghai", 3),
            }
            assert all(row.status == "queued" for row in task_rows)
            assert all(row.desired_state == "running" for row in task_rows)

            async with unit_of_work_factory() as unit_of_work:
                duplicate = CollectionJob(
                    id=uuid4(),
                    client_id=client_id,
                    external_request_id=request.external_request_id,
                    idempotency_key_hash=hash_idempotency_key(
                        SecretStr("database-conflict-key")
                    ),
                    request_payload=request.canonical_payload(),
                )
                assert not await unit_of_work.collection_jobs.try_add(duplicate)
                await unit_of_work.commit()

            await synchronizer.synchronize(build_allowlist(enabled=False))
            with pytest.raises(CallerAccessTokenAuthenticationDenied):
                await submitter.submit(
                    access_token=SecretStr(issued.access_token),
                    idempotency_key=SecretStr("disabled-client-operation"),
                    request=build_request(external_request_id="jobreq_integration_003"),
                )

            assert await connection.scalar(select(func.count()).select_from(CollectionJobRow)) == 1
            assert await connection.scalar(select(func.count()).select_from(CollectionTaskRow)) == 2

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert client_id is not None
            assert token_id is not None
            assert job_id is not None
            assert (
                await verification_connection.scalar(
                    select(ApiClientRow.id).where(ApiClientRow.id == client_id)
                )
                is None
            )
            assert (
                await verification_connection.scalar(
                    select(ApiAccessTokenRow.id).where(ApiAccessTokenRow.id == token_id)
                )
                is None
            )
            assert (
                await verification_connection.scalar(
                    select(CollectionJobRow.id).where(CollectionJobRow.id == job_id)
                )
                is None
            )
    finally:
        await engine.dispose()

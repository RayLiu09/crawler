import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import CollectionJobSubmitter
from crawler_api.application.tenant_allowlist import TenantAllowlist, TenantAllowlistSynchronizer
from crawler_api.application.webhook_dispatcher import WebhookDispatcher, WebhookHttpResponse
from crawler_api.application.webhook_operations import (
    WebhookDeliveryOperator,
    WebhookDeliveryReplayConflict,
)
from crawler_api.config import get_settings
from crawler_api.domain.webhooks import WebhookOutboxStatus
from crawler_api.infrastructure.database.models import (
    CollectionEventRow,
    WebhookOutboxReplayRow,
    WebhookOutboxRow,
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

TENANT_ID = "webhook-operations-integration-tenant"
TENANT_KEY = "webhook-operations-integration-key"
CALLER_TOKEN = "cat_webhook_operations_integration_token_123456789"
CALLBACK_TOKEN = "callback_operations_integration_token_123456789012345"
CALLBACK_URL = "http://upstream.internal/api/job-collection/events"
BASE_TIME = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)


class SuccessfulTransport:
    async def send(self, **_: object) -> WebhookHttpResponse:
        return WebhookHttpResponse(status_code=204, headers={})


def build_allowlist() -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Webhook Operations Integration Tenant",
                    "tenantKeyHashes": [
                        f"sha256:{hashlib.sha256(TENANT_KEY.encode()).hexdigest()}"
                    ],
                    "callbackAllowlist": [CALLBACK_URL],
                }
            ],
        }
    )


async def test_retry_exhausted_delivery_query_replay_and_dispatch_are_transactional() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    protector = CallbackTokenProtector(
        encryption_key=settings.callback_token_encryption_key,
        fingerprint_key=settings.callback_token_fingerprint_key,
        encryption_key_version=settings.callback_token_encryption_key_version,
    )
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
            await TenantAllowlistSynchronizer(unit_of_work_factory).synchronize(allowlist)
            issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=7200,
                clock=lambda: BASE_TIME,
                token_generator=lambda: CALLER_TOKEN,
            ).issue(SecretStr(TENANT_KEY))
            authenticator = CallerAccessTokenAuthenticator(token_hasher.hash)
            submitted = await CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                tenant_allowlist=allowlist,
                callback_token_protector=protector,
                clock=lambda: BASE_TIME + timedelta(minutes=1),
            ).submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("webhook-operations-create-001"),
                request=SubmitCollectionJobRequest.model_validate(
                    {
                        "externalRequestId": "jobreq_webhook_operations_postgres_001",
                        "source": "zhaopin",
                        "queries": [{"keyword": "Python", "region": "Shanghai"}],
                        "callback": {
                            "url": CALLBACK_URL,
                            "accessToken": CALLBACK_TOKEN,
                            "tokenType": "Bearer",
                        },
                    }
                ),
            )
            outbox_id = await connection.scalar(
                select(WebhookOutboxRow.id)
                .join(CollectionEventRow, CollectionEventRow.id == WebhookOutboxRow.event_id)
                .where(CollectionEventRow.collection_job_id == submitted.job.id)
            )
            assert outbox_id is not None
            await connection.execute(
                update(WebhookOutboxRow)
                .where(WebhookOutboxRow.id == outbox_id)
                .values(
                    status=WebhookOutboxStatus.PERMANENTLY_FAILED.value,
                    attempt_count=8,
                    last_failure_code="retry_exhausted_http_503",
                )
            )

            replayed_at = BASE_TIME + timedelta(minutes=2)
            operator = WebhookDeliveryOperator(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                clock=lambda: replayed_at,
            )
            page = await operator.list_for_job(
                access_token=SecretStr(issued.access_token),
                job_id=submitted.job.id,
                status=WebhookOutboxStatus.PERMANENTLY_FAILED,
                cursor=None,
                limit=50,
            )
            assert len(page.items) == 1
            assert page.items[0].outbox_id == outbox_id
            assert page.items[0].replay_eligible

            first = await operator.replay(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("webhook-operations-replay-001"),
                job_id=submitted.job.id,
                outbox_id=outbox_id,
            )
            repeated = await operator.replay(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("webhook-operations-replay-001"),
                job_id=submitted.job.id,
                outbox_id=outbox_id,
            )
            assert repeated == first
            with pytest.raises(WebhookDeliveryReplayConflict):
                await operator.replay(
                    access_token=SecretStr(issued.access_token),
                    idempotency_key=SecretStr("webhook-operations-replay-002"),
                    job_id=submitted.job.id,
                    outbox_id=outbox_id,
                )

            async with session_factory() as inspection_session:
                replayed = await inspection_session.get(WebhookOutboxRow, outbox_id)
                replay_history_count = await inspection_session.scalar(
                    select(func.count())
                    .select_from(WebhookOutboxReplayRow)
                    .where(WebhookOutboxReplayRow.webhook_outbox_id == outbox_id)
                )
            assert replayed is not None
            assert replayed.status == WebhookOutboxStatus.PENDING.value
            assert replayed.attempt_count == 0
            assert replayed.replay_count == 1
            assert replayed.last_replayed_at == replayed_at
            assert replay_history_count == 1

            dispatcher = WebhookDispatcher(
                unit_of_work_factory=unit_of_work_factory,
                callback_token_protector=protector,
                transport=SuccessfulTransport(),
                claim_lease_seconds=30,
                max_attempts=8,
                retry_base_seconds=5,
                retry_max_seconds=900,
                retry_after_max_seconds=300,
                clock=lambda: replayed_at,
            )
            assert await dispatcher.dispatch_one()
            async with session_factory() as inspection_session:
                delivered = await inspection_session.get(WebhookOutboxRow, outbox_id)
            assert delivered is not None
            assert delivered.status == WebhookOutboxStatus.DELIVERED.value
            assert delivered.attempt_count == 1
            assert delivered.replay_count == 1

            await outer_transaction.rollback()
    finally:
        await engine.dispose()

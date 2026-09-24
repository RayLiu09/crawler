import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.callback_token_rotation import (
    CollectionJobCallbackTokenRotator,
)
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import CollectionJobSubmitter
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.application.webhook_dispatcher import (
    WebhookDispatcher,
    WebhookHttpResponse,
)
from crawler_api.config import get_settings
from crawler_api.domain.enums import CollectionState
from crawler_api.domain.webhooks import (
    WebhookDeliverySettlement,
    WebhookOutboxStatus,
)
from crawler_api.infrastructure.database.models import (
    CollectionEventRow,
    CollectionJobCallbackConfigRow,
    CollectionJobRow,
    WebhookOutboxRow,
)
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import (
    RotateCollectionJobCallbackTokenRequest,
    SubmitCollectionJobRequest,
)
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "webhook-dispatcher-integration-tenant"
TENANT_KEY = "webhook-dispatcher-integration-key"
CALLER_TOKEN = "cat_webhook_dispatcher_integration_token_123456789"
CALLBACK_TOKEN = "callback_dispatcher_integration_token_123456789012345"
ROTATED_CALLBACK_TOKEN = "callback_dispatcher_rotated_token_987654321098765"
CALLBACK_URL = "http://upstream.internal/api/job-collection/events"
BASE_TIME = datetime(2026, 9, 17, 19, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SequenceTransport:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.tokens: list[str] = []

    async def send(self, **kwargs: object) -> WebhookHttpResponse:
        token = kwargs["access_token"]
        assert isinstance(token, SecretStr)
        self.tokens.append(token.get_secret_value())
        return WebhookHttpResponse(status_code=self.statuses.pop(0), headers={})


def build_allowlist() -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Webhook Dispatcher Integration Tenant",
                    "tenantKeyHashes": [
                        f"sha256:{hashlib.sha256(TENANT_KEY.encode()).hexdigest()}"
                    ],
                    "callbackAllowlist": [CALLBACK_URL],
                }
            ],
        }
    )


def job_request(index: int) -> SubmitCollectionJobRequest:
    return SubmitCollectionJobRequest.model_validate(
        {
            "externalRequestId": f"jobreq_webhook_dispatcher_{index:03d}",
            "source": "zhaopin",
            "queries": [{"keyword": "Python", "region": "Shanghai"}],
            "callback": {
                "url": CALLBACK_URL,
                "accessToken": CALLBACK_TOKEN,
                "tokenType": "Bearer",
            },
        }
    )


async def test_dispatch_claims_retries_and_credential_races_are_transactional() -> None:
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
            submitter = CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                tenant_allowlist=allowlist,
                callback_token_protector=protector,
                clock=lambda: BASE_TIME + timedelta(minutes=1),
            )

            first_job = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("webhook-dispatcher-create-001"),
                request=job_request(1),
            )
            clock = MutableClock(BASE_TIME + timedelta(minutes=2))
            transport = SequenceTransport([503, 204])
            dispatcher = WebhookDispatcher(
                unit_of_work_factory=unit_of_work_factory,
                callback_token_protector=protector,
                transport=transport,
                claim_lease_seconds=30,
                max_attempts=8,
                retry_base_seconds=5,
                retry_max_seconds=900,
                retry_after_max_seconds=300,
                clock=clock,
            )

            assert await dispatcher.dispatch_one()
            async with session_factory() as inspection_session:
                first_outbox = await inspection_session.scalar(
                    select(WebhookOutboxRow)
                    .join(
                        CollectionEventRow,
                        CollectionEventRow.id == WebhookOutboxRow.event_id,
                    )
                    .where(CollectionEventRow.collection_job_id == first_job.job.id)
                )
            assert first_outbox is not None
            assert first_outbox.status == WebhookOutboxStatus.PENDING.value
            assert first_outbox.attempt_count == 1
            assert first_outbox.next_attempt_at == clock.now + timedelta(seconds=5)
            assert first_outbox.dispatch_claim_id is None
            async with unit_of_work_factory() as unit_of_work:
                pending_metrics = await unit_of_work.webhook_outbox.get_metrics_snapshot(
                    clock.now
                )
                await unit_of_work.commit()
            assert pending_metrics.pending_count == 1
            assert pending_metrics.oldest_pending_created_at is not None
            assert pending_metrics.credential_blocked_count == 0
            assert pending_metrics.permanently_failed_count == 0
            assert pending_metrics.retry_exhausted_count == 0
            assert pending_metrics.active_claim_count == 0

            await connection.execute(
                update(CollectionJobRow)
                .where(CollectionJobRow.id == first_job.job.id)
                .values(status=CollectionState.SUCCEEDED.value)
            )
            clock.now = first_outbox.next_attempt_at
            assert await dispatcher.dispatch_one()
            async with session_factory() as inspection_session:
                delivered = await inspection_session.get(WebhookOutboxRow, first_outbox.id)
                destroyed_config = await inspection_session.scalar(
                    select(CollectionJobCallbackConfigRow).where(
                        CollectionJobCallbackConfigRow.collection_job_id == first_job.job.id
                    )
                )
            assert delivered is not None
            assert destroyed_config is not None
            assert delivered.status == WebhookOutboxStatus.DELIVERED.value
            assert delivered.attempt_count == 2
            assert delivered.delivered_at == clock.now
            assert destroyed_config.token_ciphertext is None
            assert destroyed_config.token_nonce is None
            assert destroyed_config.credential_destroyed_at == clock.now
            assert destroyed_config.token_fingerprint.startswith("hmac-sha256:")
            assert destroyed_config.credential_version == 1
            assert transport.tokens == [CALLBACK_TOKEN, CALLBACK_TOKEN]

            second_job = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("webhook-dispatcher-create-002"),
                request=job_request(2),
            )
            claim_time = BASE_TIME + timedelta(minutes=3)
            first_claim_id = uuid4()
            async with unit_of_work_factory() as unit_of_work:
                first_claim = await unit_of_work.webhook_outbox.claim_due(
                    claim_id=first_claim_id,
                    claimed_at=claim_time,
                    lease_expires_at=claim_time + timedelta(seconds=30),
                )
                await unit_of_work.commit()
            assert first_claim is not None
            assert first_claim.event.collection_job_id == second_job.job.id

            async with unit_of_work_factory() as unit_of_work:
                duplicate_claim = await unit_of_work.webhook_outbox.claim_due(
                    claim_id=uuid4(),
                    claimed_at=claim_time,
                    lease_expires_at=claim_time + timedelta(seconds=30),
                )
                await unit_of_work.commit()
            assert duplicate_claim is None

            second_claim_id = uuid4()
            async with unit_of_work_factory() as unit_of_work:
                recovered_claim = await unit_of_work.webhook_outbox.claim_due(
                    claim_id=second_claim_id,
                    claimed_at=claim_time + timedelta(seconds=30),
                    lease_expires_at=claim_time + timedelta(seconds=60),
                )
                await unit_of_work.commit()
            assert recovered_claim is not None
            assert recovered_claim.outbox_id == first_claim.outbox_id
            assert recovered_claim.recovered_expired_claim
            async with unit_of_work_factory() as unit_of_work:
                claimed_metrics = await unit_of_work.webhook_outbox.get_metrics_snapshot(
                    claim_time + timedelta(seconds=30)
                )
                await unit_of_work.commit()
            assert claimed_metrics.pending_count == 1
            assert claimed_metrics.active_claim_count == 1

            stale_settlement = WebhookDeliverySettlement(
                outbox_id=first_claim.outbox_id,
                claim_id=first_claim.claim_id,
                claimed_credential_version=1,
                status=WebhookOutboxStatus.DELIVERED,
                settled_at=claim_time + timedelta(seconds=31),
            )
            async with unit_of_work_factory() as unit_of_work:
                assert not await unit_of_work.webhook_outbox.settle_claim(stale_settlement)
                await unit_of_work.commit()

            async with unit_of_work_factory() as unit_of_work:
                recovered_settlement = await unit_of_work.webhook_outbox.settle_claim(
                    WebhookDeliverySettlement(
                        outbox_id=recovered_claim.outbox_id,
                        claim_id=recovered_claim.claim_id,
                        claimed_credential_version=1,
                        status=WebhookOutboxStatus.DELIVERED,
                        settled_at=claim_time + timedelta(seconds=32),
                    )
                )
                await unit_of_work.commit()
            assert recovered_settlement is not None
            assert recovered_settlement.status is WebhookOutboxStatus.DELIVERED
            async with session_factory() as inspection_session:
                active_config = await inspection_session.scalar(
                    select(CollectionJobCallbackConfigRow).where(
                        CollectionJobCallbackConfigRow.collection_job_id == second_job.job.id
                    )
                )
            assert active_config is not None
            assert active_config.token_ciphertext is not None
            assert active_config.token_nonce is not None
            assert active_config.credential_destroyed_at is None

            third_job = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("webhook-dispatcher-create-003"),
                request=job_request(3),
            )
            third_claim_id = uuid4()
            async with unit_of_work_factory() as unit_of_work:
                third_claim = await unit_of_work.webhook_outbox.claim_due(
                    claim_id=third_claim_id,
                    claimed_at=claim_time,
                    lease_expires_at=claim_time + timedelta(seconds=30),
                )
                await unit_of_work.commit()
            assert third_claim is not None
            assert third_claim.event.collection_job_id == third_job.job.id

            await CollectionJobCallbackTokenRotator(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                callback_token_protector=protector,
                clock=lambda: claim_time + timedelta(seconds=1),
            ).rotate(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("webhook-dispatcher-rotate-003"),
                job_id=third_job.job.id,
                request=RotateCollectionJobCallbackTokenRequest(
                    accessToken=ROTATED_CALLBACK_TOKEN,
                    tokenType="Bearer",
                ),
            )

            async with unit_of_work_factory() as unit_of_work:
                credential_race_settlement = (
                    await unit_of_work.webhook_outbox.settle_claim(
                        WebhookDeliverySettlement(
                            outbox_id=third_claim.outbox_id,
                            claim_id=third_claim.claim_id,
                            claimed_credential_version=1,
                            status=WebhookOutboxStatus.CREDENTIAL_BLOCKED,
                            settled_at=claim_time + timedelta(seconds=2),
                            failure_code="http_401",
                        )
                    )
                )
                await unit_of_work.commit()
            assert credential_race_settlement is not None
            assert credential_race_settlement.status is WebhookOutboxStatus.PENDING
            assert credential_race_settlement.failure_code is None
            async with session_factory() as inspection_session:
                raced = await inspection_session.get(WebhookOutboxRow, third_claim.outbox_id)
            assert raced is not None
            assert raced.status == WebhookOutboxStatus.PENDING.value
            assert raced.last_failure_code is None
            assert raced.next_attempt_at == claim_time + timedelta(seconds=2)
            assert raced.attempt_count == 1
            assert raced.dispatch_claim_id is None

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert (
                await verification_connection.scalar(
                    select(func.count()).select_from(WebhookOutboxRow)
                )
                == 0
            )
    finally:
        await engine.dispose()

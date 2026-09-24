import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import (
    CollectionJobIdempotencyConflict,
    CollectionJobSubmitter,
)
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import (
    CollectionJobCallbackConfigRow,
    CollectionJobRow,
    WebhookOutboxRow,
)
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.callback_tokens import (
    CallbackTokenProtector,
    ProtectedCallbackToken,
)
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "collection-job-callback-integration-tenant"
TENANT_KEY = "collection-job-callback-integration-key"
ACCESS_TOKEN = "cat_collection_job_callback_integration_token"
CALLBACK_URL = "http://upstream.internal/api/job-collection/events"
CALLBACK_TOKEN = "callback_token_12345678901234567890123456789"
CHANGED_CALLBACK_TOKEN = "callback_token_98765432109876543210987654321"
BASE_TIME = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


def build_allowlist() -> TenantAllowlist:
    key_hash = hashlib.sha256(TENANT_KEY.encode()).hexdigest()
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Collection callback integration tenant",
                    "tenantKeyHashes": [f"sha256:{key_hash}"],
                    "callbackAllowlist": [CALLBACK_URL],
                }
            ],
        }
    )


def build_request(
    *,
    external_request_id: str = "jobreq_callback_integration_001",
    callback_token: str | None = CALLBACK_TOKEN,
) -> SubmitCollectionJobRequest:
    payload: dict[str, object] = {
        "externalRequestId": external_request_id,
        "source": "zhaopin",
        "queries": [{"keyword": "Python", "region": "Shanghai"}],
    }
    if callback_token is not None:
        payload["callback"] = {
            "url": CALLBACK_URL,
            "accessToken": callback_token,
            "tokenType": "Bearer",
        }
    return SubmitCollectionJobRequest.model_validate(payload)


async def test_callback_config_is_encrypted_idempotent_and_rolled_back() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    protector = CallbackTokenProtector(
        encryption_key=settings.callback_token_encryption_key,
        fingerprint_key=settings.callback_token_fingerprint_key,
        encryption_key_version=settings.callback_token_encryption_key_version,
    )
    callback_config_id: UUID | None = None
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
                ttl_seconds=3600,
                clock=lambda: BASE_TIME,
                token_generator=lambda: ACCESS_TOKEN,
            ).issue(SecretStr(TENANT_KEY))
            submitter = CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=CallerAccessTokenAuthenticator(token_hasher.hash),
                tenant_allowlist=allowlist,
                callback_token_protector=protector,
                clock=lambda: BASE_TIME,
            )
            request = build_request()

            first = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("callback-integration-operation"),
                request=request,
            )
            replay = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("callback-integration-operation"),
                request=request,
            )

            assert first.created
            assert not replay.created
            assert first.callback_token_expires_at == BASE_TIME + timedelta(days=7)
            assert replay.callback_token_expires_at == first.callback_token_expires_at

            with pytest.raises(CollectionJobIdempotencyConflict):
                await submitter.submit(
                    access_token=SecretStr(issued.access_token),
                    idempotency_key=SecretStr("callback-integration-operation"),
                    request=build_request(callback_token=CHANGED_CALLBACK_TOKEN),
                )

            without_callback = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("no-callback-integration-operation"),
                request=build_request(
                    external_request_id="jobreq_callback_integration_002",
                    callback_token=None,
                ),
            )
            assert without_callback.callback_token_expires_at is None

            callback_row = (
                await connection.execute(
                    select(
                        CollectionJobCallbackConfigRow.id,
                        CollectionJobCallbackConfigRow.collection_job_id,
                        CollectionJobCallbackConfigRow.token_ciphertext,
                        CollectionJobCallbackConfigRow.token_nonce,
                        CollectionJobCallbackConfigRow.token_fingerprint,
                        CollectionJobCallbackConfigRow.token_expires_at,
                        CollectionJobCallbackConfigRow.credential_version,
                        CollectionJobCallbackConfigRow.encryption_key_version,
                    )
                )
            ).one()
            callback_config_id = callback_row.id
            job_payload = await connection.scalar(
                select(CollectionJobRow.request_payload).where(
                    CollectionJobRow.id == first.job.id
                )
            )
            column_names = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'crawler' "
                            "AND table_name = 'collection_job_callback_configs'"
                        )
                    )
                ).scalars()
            )
            outbox_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'crawler' "
                            "AND table_name = 'webhook_outbox'"
                        )
                    )
                ).scalars()
            )
            outbox_row = (
                await connection.execute(
                    select(
                        WebhookOutboxRow.event_id,
                        WebhookOutboxRow.callback_config_id,
                        WebhookOutboxRow.attempt_count,
                        WebhookOutboxRow.next_attempt_at,
                    )
                )
            ).one()

            assert job_payload is not None
            assert "callback" not in job_payload
            assert CALLBACK_TOKEN not in str(job_payload)
            assert column_names.isdisjoint({"access_token", "token_plaintext"})
            assert "target_url" not in outbox_columns
            assert "callback_config_id" in outbox_columns
            assert CALLBACK_TOKEN.encode() not in callback_row.token_ciphertext
            assert len(callback_row.token_nonce) == 12
            assert callback_row.token_fingerprint == protector.fingerprint(
                SecretStr(CALLBACK_TOKEN)
            )
            assert callback_row.token_expires_at == BASE_TIME + timedelta(days=7)
            assert callback_row.credential_version == 1
            assert (
                callback_row.encryption_key_version
                == settings.callback_token_encryption_key_version
            )
            decrypted = protector.decrypt(
                ProtectedCallbackToken(
                    ciphertext=callback_row.token_ciphertext,
                    nonce=callback_row.token_nonce,
                    fingerprint=callback_row.token_fingerprint,
                    encryption_key_version=callback_row.encryption_key_version,
                ),
                callback_config_id=callback_row.id,
                collection_job_id=callback_row.collection_job_id,
                credential_version=callback_row.credential_version,
            )
            assert decrypted.get_secret_value() == CALLBACK_TOKEN
            assert outbox_row.event_id is not None
            assert outbox_row.callback_config_id == callback_row.id
            assert outbox_row.attempt_count == 0
            assert outbox_row.next_attempt_at == BASE_TIME
            assert (
                await connection.scalar(
                    select(func.count()).select_from(CollectionJobCallbackConfigRow)
                )
                == 1
            )
            assert (
                await connection.scalar(
                    select(func.count()).select_from(WebhookOutboxRow)
                )
                == 1
            )

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert callback_config_id is not None
            assert (
                await verification_connection.scalar(
                    select(CollectionJobCallbackConfigRow.id).where(
                        CollectionJobCallbackConfigRow.id == callback_config_id
                    )
                )
                is None
            )
    finally:
        await engine.dispose()

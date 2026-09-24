import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.callback_token_rotation import (
    CallbackTokenRotationConflict,
    CollectionJobCallbackTokenRotator,
)
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import CollectionJobSubmitter
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.domain.webhooks import WebhookOutboxStatus
from crawler_api.infrastructure.database.models import (
    CollectionJobCallbackConfigRow,
    CollectionJobCallbackCredentialRotationRow,
    WebhookOutboxRow,
)
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.callback_tokens import (
    CallbackTokenProtector,
    ProtectedCallbackToken,
)
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import (
    RotateCollectionJobCallbackTokenRequest,
    SubmitCollectionJobRequest,
)
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "callback-rotation-integration-tenant"
TENANT_KEY = "callback-rotation-integration-tenant-key"
CALLER_TOKEN = "cat_callback_rotation_integration_token_123456789"
CALLBACK_TOKEN = "callback_rotation_integration_token_123456789012345"
SECOND_CALLBACK_TOKEN = "callback_rotation_integration_token_987654321098765"
CALLBACK_URL = "http://upstream.internal/api/job-collection/events"
BASE_TIME = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)


def build_allowlist() -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Callback Rotation Integration Tenant",
                    "tenantKeyHashes": [
                        f"sha256:{hashlib.sha256(TENANT_KEY.encode()).hexdigest()}"
                    ],
                    "callbackAllowlist": [CALLBACK_URL],
                }
            ],
        }
    )


def rotation_request(token: str) -> RotateCollectionJobCallbackTokenRequest:
    return RotateCollectionJobCallbackTokenRequest(
        accessToken=token,
        tokenType="Bearer",
    )


async def test_callback_token_rotation_is_versioned_idempotent_and_rolled_back() -> None:
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
            await TenantAllowlistSynchronizer(unit_of_work_factory).synchronize(
                allowlist
            )
            issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=7200,
                clock=lambda: BASE_TIME,
                token_generator=lambda: CALLER_TOKEN,
            ).issue(SecretStr(TENANT_KEY))
            authenticator = CallerAccessTokenAuthenticator(token_hasher.hash)
            request = SubmitCollectionJobRequest.model_validate(
                {
                    "externalRequestId": "jobreq_callback_rotation_integration_001",
                    "source": "zhaopin",
                    "queries": [{"keyword": "Python", "region": "Shanghai"}],
                    "callback": {
                        "url": CALLBACK_URL,
                        "accessToken": CALLBACK_TOKEN,
                        "tokenType": "Bearer",
                    },
                }
            )
            submitted = await CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                tenant_allowlist=allowlist,
                callback_token_protector=protector,
                clock=lambda: BASE_TIME + timedelta(minutes=1),
            ).submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("callback-rotation-create"),
                request=request,
            )
            job_id = submitted.job.id

            async with session_factory() as inspection_session:
                original = await inspection_session.scalar(
                    select(CollectionJobCallbackConfigRow).where(
                        CollectionJobCallbackConfigRow.collection_job_id == job_id
                    )
                )
            assert original is not None
            original_url = original.target_url
            original_ciphertext = original.token_ciphertext
            await connection.execute(
                update(WebhookOutboxRow)
                .where(WebhookOutboxRow.callback_config_id == original.id)
                .values(
                    status=WebhookOutboxStatus.CREDENTIAL_BLOCKED.value,
                    last_failure_code="http_401",
                )
            )

            rotator = CollectionJobCallbackTokenRotator(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                callback_token_protector=protector,
                clock=lambda: BASE_TIME + timedelta(minutes=2),
            )
            first = await rotator.rotate(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("callback-rotation-001"),
                job_id=job_id,
                request=rotation_request(SECOND_CALLBACK_TOKEN),
            )
            async with session_factory() as inspection_session:
                stored = await inspection_session.scalar(
                    select(CollectionJobCallbackConfigRow).where(
                        CollectionJobCallbackConfigRow.collection_job_id == job_id
                    )
                )
            assert stored is not None
            assert first.credential_version == 2
            assert stored.credential_version == 2
            assert stored.target_url == original_url
            assert stored.token_ciphertext != original_ciphertext
            assert stored.token_expires_at == BASE_TIME + timedelta(days=7, minutes=2)
            protected = ProtectedCallbackToken(
                ciphertext=stored.token_ciphertext,
                nonce=stored.token_nonce,
                fingerprint=stored.token_fingerprint,
                encryption_key_version=stored.encryption_key_version,
            )
            assert (
                protector.decrypt(
                    protected,
                    callback_config_id=stored.id,
                    collection_job_id=job_id,
                    credential_version=2,
                ).get_secret_value()
                == SECOND_CALLBACK_TOKEN
            )
            with pytest.raises(InvalidTag):
                protector.decrypt(
                    protected,
                    callback_config_id=stored.id,
                    collection_job_id=job_id,
                    credential_version=1,
                )

            async with session_factory() as inspection_session:
                outbox = await inspection_session.scalar(
                    select(WebhookOutboxRow).where(
                        WebhookOutboxRow.callback_config_id == stored.id
                    )
                )
            assert outbox is not None
            assert outbox.status == WebhookOutboxStatus.PENDING.value
            assert outbox.next_attempt_at == BASE_TIME + timedelta(minutes=2)
            assert outbox.last_failure_code is None

            async with session_factory() as inspection_session:
                rotations = (
                    await inspection_session.scalars(
                        select(CollectionJobCallbackCredentialRotationRow)
                    )
                ).all()
            assert len(rotations) == 1
            assert rotations[0].result_credential_version == 2
            assert CALLBACK_TOKEN not in repr(rotations[0].__dict__)
            assert SECOND_CALLBACK_TOKEN not in repr(rotations[0].__dict__)

            replay = await rotator.rotate(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("callback-rotation-001"),
                job_id=job_id,
                request=rotation_request(SECOND_CALLBACK_TOKEN),
            )
            assert replay == first
            assert (
                await connection.scalar(
                    select(func.count()).select_from(
                        CollectionJobCallbackCredentialRotationRow
                    )
                )
                == 1
            )

            with pytest.raises(CallbackTokenRotationConflict):
                await rotator.rotate(
                    access_token=SecretStr(issued.access_token),
                    idempotency_key=SecretStr("callback-rotation-001"),
                    job_id=job_id,
                    request=rotation_request(CALLBACK_TOKEN),
                )

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert (
                await verification_connection.scalar(
                    select(func.count()).select_from(
                        CollectionJobCallbackCredentialRotationRow
                    )
                )
                == 0
            )
    finally:
        await engine.dispose()

import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import ApiAccessTokenRow, ApiClientRow
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "access-token-integration-tenant"
TENANT_KEY = "access-token-integration-tenant-key"
PLAINTEXT_TOKEN = "cat_integration_plaintext_token_1234567890"
ISSUED_AT = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


def build_allowlist() -> TenantAllowlist:
    key_fingerprint = hashlib.sha256(TENANT_KEY.encode()).hexdigest()
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": TENANT_ID,
                    "displayName": "Access token integration tenant",
                    "tenantKeyHashes": [f"sha256:{key_fingerprint}"],
                    "scopes": ["collection:read"],
                }
            ],
        }
    )


async def test_access_token_issuance_persists_snapshot_then_rolls_back() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    allowlist = build_allowlist()
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    client_id = None
    token_id = None
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
            await synchronizer.synchronize(allowlist)

            issuer = AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=3600,
                clock=lambda: ISSUED_AT,
                token_generator=lambda: PLAINTEXT_TOKEN,
            )
            issued = await issuer.issue(SecretStr(TENANT_KEY))

            client_row = (
                await connection.execute(
                    select(
                        ApiClientRow.id,
                        ApiClientRow.last_token_issued_at,
                    ).where(ApiClientRow.tenant_id == TENANT_ID)
                )
            ).one()
            token_row = (
                await connection.execute(
                    select(
                        ApiAccessTokenRow.id,
                        ApiAccessTokenRow.token_hash,
                        ApiAccessTokenRow.scopes,
                        ApiAccessTokenRow.issued_via_key_fingerprint,
                        ApiAccessTokenRow.issued_at,
                        ApiAccessTokenRow.expires_at,
                    ).where(ApiAccessTokenRow.client_id == client_row.id)
                )
            ).one()

            client_id = client_row.id
            token_id = token_row.id
            assert issued.access_token == PLAINTEXT_TOKEN
            assert issued.scopes == ["collection:read"]
            assert token_row.token_hash == token_hasher.hash(PLAINTEXT_TOKEN)
            assert token_row.token_hash != PLAINTEXT_TOKEN
            assert token_row.scopes == ["collection:read"]
            assert token_row.issued_via_key_fingerprint == (
                f"sha256:{hashlib.sha256(TENANT_KEY.encode()).hexdigest()}"
            )
            assert token_row.issued_at == ISSUED_AT
            assert token_row.expires_at == ISSUED_AT + timedelta(hours=1)
            assert client_row.last_token_issued_at == ISSUED_AT

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert client_id is not None
            assert token_id is not None
            assert await verification_connection.get_isolation_level() is not None
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
    finally:
        await engine.dispose()

import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import ApiAccessTokenRow, ApiClientRow
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)


def build_allowlist(*, enabled: bool) -> TenantAllowlist:
    key_hash = hashlib.sha256(b"integration-tenant-key").hexdigest()
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": "allowlist-integration-tenant",
                    "displayName": "Allowlist integration tenant",
                    "enabled": enabled,
                    "tenantKeyHashes": [f"sha256:{key_hash}"],
                }
            ],
        }
    )


async def test_allowlist_sync_disables_client_and_revokes_token_then_rolls_back() -> None:
    engine = create_database_engine(get_settings())
    client_id = None
    token_id = uuid4()
    expired_token_id = uuid4()
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
            synchronizer = TenantAllowlistSynchronizer(
                lambda: SqlAlchemyUnitOfWork(session_factory)
            )

            active_result = await synchronizer.synchronize(build_allowlist(enabled=True))
            client_id = await connection.scalar(
                select(ApiClientRow.id).where(
                    ApiClientRow.tenant_id == "allowlist-integration-tenant"
                )
            )
            assert client_id is not None
            assert active_result.created_clients == 1

            await connection.execute(
                insert(ApiAccessTokenRow).values(
                    id=token_id,
                    client_id=client_id,
                    token_hash="allowlist-integration-token-hash",
                    issued_via_key_fingerprint="sha256:" + "2" * 64,
                    scopes=["collection:read"],
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await connection.execute(
                insert(ApiAccessTokenRow).values(
                    id=expired_token_id,
                    client_id=client_id,
                    token_hash="allowlist-integration-expired-token-hash",
                    issued_via_key_fingerprint="sha256:" + "3" * 64,
                    scopes=["collection:read"],
                    expires_at=datetime.now(UTC) - timedelta(hours=1),
                )
            )

            disabled_result = await synchronizer.synchronize(build_allowlist(enabled=False))
            status = await connection.scalar(
                select(ApiClientRow.status).where(ApiClientRow.id == client_id)
            )
            revoked_at = await connection.scalar(
                select(ApiAccessTokenRow.revoked_at).where(ApiAccessTokenRow.id == token_id)
            )
            expired_revoked_at = await connection.scalar(
                select(ApiAccessTokenRow.revoked_at).where(
                    ApiAccessTokenRow.id == expired_token_id
                )
            )

            assert disabled_result.updated_clients == 1
            assert disabled_result.disabled_clients == 1
            assert disabled_result.revoked_tokens == 1
            assert status == "disabled"
            assert revoked_at is not None
            assert expired_revoked_at is None

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
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

import os
from uuid import UUID

import pytest
from crawler_api.application.agent_provisioning import AgentProvisioner
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import CollectionAgentRow
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

AGENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
DEVICE_SECRET = "dev_integration_device_secret"


async def test_provision_agent_persists_offline_identity_then_rolls_back() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
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

            provisioner = AgentProvisioner(
                unit_of_work_factory=unit_of_work_factory,
                hash_device_secret=DeviceCredentialHasher(
                    settings.agent_device_credential_hash_key
                ).hash,
                device_secret_generator=lambda: DEVICE_SECRET,
            )
            result = await provisioner.provision(
                display_name="Integration desktop Agent",
                agent_id=AGENT_ID,
            )

            row = (
                await connection.execute(
                    select(
                        CollectionAgentRow.id,
                        CollectionAgentRow.device_credential_hash,
                        CollectionAgentRow.display_name,
                        CollectionAgentRow.status,
                        CollectionAgentRow.capabilities,
                    ).where(CollectionAgentRow.id == AGENT_ID)
                )
            ).one()

            assert result.agent_id == AGENT_ID
            assert result.device_secret == DEVICE_SECRET
            assert row.id == AGENT_ID
            assert row.display_name == "Integration desktop Agent"
            assert row.status == "offline"
            assert row.device_credential_hash == DeviceCredentialHasher(
                settings.agent_device_credential_hash_key
            ).hash(DEVICE_SECRET)
            assert row.capabilities == {
                "schemaVersion": 1,
                "supportedSources": [],
                "maxConcurrentTasks": 1,
                "browser": {"name": "chrome", "headful": True},
            }

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert (
                await verification_connection.scalar(
                    select(CollectionAgentRow.id).where(CollectionAgentRow.id == AGENT_ID)
                )
                is None
            )
    finally:
        await engine.dispose()

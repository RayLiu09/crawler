import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.application.agent_credentials import AgentCredentialManager
from crawler_api.application.agent_provisioning import AgentProvisioner
from crawler_api.application.agent_sessions import (
    AgentAuthenticationDenied,
    AgentSessionIssuer,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import AgentSessionRow, CollectionAgentRow
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

AGENT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
INITIAL_DEVICE_SECRET = "dev_integration_initial_device_secret"
ROTATED_DEVICE_SECRET = "dev_integration_rotated_device_secret"
FIRST_SESSION_TOKEN = "agt_integration_first_session_token"
SECOND_SESSION_TOKEN = "agt_integration_second_session_token"
ISSUED_AT = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
ROTATED_AT = ISSUED_AT + timedelta(minutes=1)
DISABLED_AT = ISSUED_AT + timedelta(minutes=2)


async def test_agent_identity_lifecycle_is_atomic_and_rolled_back() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    device_hasher = DeviceCredentialHasher(settings.agent_device_credential_hash_key)
    session_hasher = AgentSessionTokenHasher(settings.agent_token_hash_key)
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
                hash_device_secret=device_hasher.hash,
                device_secret_generator=lambda: INITIAL_DEVICE_SECRET,
            )
            await provisioner.provision(
                display_name="Agent identity integration node",
                agent_id=AGENT_ID,
            )

            initial_issuer = AgentSessionIssuer(
                unit_of_work_factory=unit_of_work_factory,
                verify_device_secret=device_hasher.verify,
                hash_session_token=session_hasher.hash,
                ttl_seconds=900,
                clock=lambda: ISSUED_AT,
                token_generator=lambda: FIRST_SESSION_TOKEN,
            )
            first_session = await initial_issuer.issue(
                AGENT_ID, SecretStr(INITIAL_DEVICE_SECRET)
            )

            credential_manager = AgentCredentialManager(
                unit_of_work_factory=unit_of_work_factory,
                hash_device_secret=device_hasher.hash,
                clock=lambda: ROTATED_AT,
                device_secret_generator=lambda: ROTATED_DEVICE_SECRET,
            )
            rotation = await credential_manager.rotate(AGENT_ID)

            assert first_session.session_token == FIRST_SESSION_TOKEN
            assert first_session.expires_at == ISSUED_AT + timedelta(seconds=900)
            assert rotation.device_secret == ROTATED_DEVICE_SECRET
            assert rotation.revoked_sessions == 1

            with pytest.raises(AgentAuthenticationDenied):
                await initial_issuer.issue(AGENT_ID, SecretStr(INITIAL_DEVICE_SECRET))

            rotated_issuer = AgentSessionIssuer(
                unit_of_work_factory=unit_of_work_factory,
                verify_device_secret=device_hasher.verify,
                hash_session_token=session_hasher.hash,
                ttl_seconds=900,
                clock=lambda: ROTATED_AT,
                token_generator=lambda: SECOND_SESSION_TOKEN,
            )
            second_session = await rotated_issuer.issue(
                AGENT_ID, SecretStr(ROTATED_DEVICE_SECRET)
            )
            disable_manager = AgentCredentialManager(
                unit_of_work_factory=unit_of_work_factory,
                hash_device_secret=device_hasher.hash,
                clock=lambda: DISABLED_AT,
            )
            disabled = await disable_manager.disable(AGENT_ID)

            assert second_session.session_token == SECOND_SESSION_TOKEN
            assert disabled.revoked_sessions == 1

            with pytest.raises(AgentAuthenticationDenied):
                await rotated_issuer.issue(AGENT_ID, SecretStr(ROTATED_DEVICE_SECRET))

            agent_row = (
                await connection.execute(
                    select(
                        CollectionAgentRow.status,
                        CollectionAgentRow.device_credential_hash,
                    ).where(CollectionAgentRow.id == AGENT_ID)
                )
            ).one()
            session_rows = (
                await connection.execute(
                    select(
                        AgentSessionRow.session_token_hash,
                        AgentSessionRow.issued_at,
                        AgentSessionRow.expires_at,
                        AgentSessionRow.revoked_at,
                    )
                    .where(AgentSessionRow.agent_id == AGENT_ID)
                    .order_by(AgentSessionRow.issued_at)
                )
            ).all()

            assert agent_row.status == "disabled"
            assert agent_row.device_credential_hash == device_hasher.hash(
                ROTATED_DEVICE_SECRET
            )
            assert [row.session_token_hash for row in session_rows] == [
                session_hasher.hash(FIRST_SESSION_TOKEN),
                session_hasher.hash(SECOND_SESSION_TOKEN),
            ]
            assert all(
                row.session_token_hash
                not in {FIRST_SESSION_TOKEN, SECOND_SESSION_TOKEN}
                for row in session_rows
            )
            assert session_rows[0].revoked_at == ROTATED_AT
            assert session_rows[1].revoked_at == DISABLED_AT

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert (
                await verification_connection.scalar(
                    select(CollectionAgentRow.id).where(CollectionAgentRow.id == AGENT_ID)
                )
                is None
            )
            assert (
                await verification_connection.scalar(
                    select(AgentSessionRow.id).where(AgentSessionRow.agent_id == AGENT_ID)
                )
                is None
            )
    finally:
        await engine.dispose()

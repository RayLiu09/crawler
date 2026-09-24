import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.application.agent_authentication import (
    AgentSessionAuthenticationDenied,
    AgentSessionAuthenticator,
)
from crawler_api.application.agent_credentials import AgentCredentialManager
from crawler_api.application.agent_heartbeats import AgentHeartbeatRecorder
from crawler_api.application.agent_provisioning import AgentProvisioner
from crawler_api.application.agent_sessions import AgentSessionIssuer
from crawler_api.config import get_settings
from crawler_api.domain.entities import BrowserProfile
from crawler_api.infrastructure.database.models import (
    AgentSessionRow,
    BrowserProfileRow,
    CollectionAgentRow,
)
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from crawler_contracts.agent_protocol import AgentHeartbeatRequest
from crawler_contracts.common import SourceCode
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

AGENT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
DEVICE_SECRET = "dev_heartbeat_integration_device_secret"
SESSION_TOKEN = "agt_heartbeat_integration_session_token"
HEARTBEAT_AT = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)


def build_heartbeat() -> AgentHeartbeatRequest:
    return AgentHeartbeatRequest.model_validate(
        {
            "agentId": str(AGENT_ID),
            "capabilities": {
                "schemaVersion": 1,
                "supportedSources": ["zhaopin"],
                "maxConcurrentTasks": 1,
                "browser": {"name": "chrome", "headful": True},
            },
            "availableSlots": 1,
            "profiles": [
                {"source": "zhaopin", "profileKey": "primary", "status": "ready"}
            ],
        }
    )


async def test_authenticated_heartbeat_persists_and_disabled_session_is_rejected() -> None:
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

            await AgentProvisioner(
                unit_of_work_factory=unit_of_work_factory,
                hash_device_secret=device_hasher.hash,
                device_secret_generator=lambda: DEVICE_SECRET,
            ).provision(display_name="Heartbeat integration Agent", agent_id=AGENT_ID)

            await AgentSessionIssuer(
                unit_of_work_factory=unit_of_work_factory,
                verify_device_secret=device_hasher.verify,
                hash_session_token=session_hasher.hash,
                ttl_seconds=900,
                clock=lambda: HEARTBEAT_AT - timedelta(minutes=1),
                token_generator=lambda: SESSION_TOKEN,
            ).issue(AGENT_ID, SecretStr(DEVICE_SECRET))

            async with unit_of_work_factory() as unit_of_work:
                for profile_key, status, updated_at in (
                    ("primary", "login_required", HEARTBEAT_AT - timedelta(minutes=5)),
                    ("stale", "ready", HEARTBEAT_AT - timedelta(seconds=181)),
                    ("fresh", "busy", HEARTBEAT_AT - timedelta(seconds=30)),
                ):
                    await unit_of_work.browser_profiles.upsert(
                        BrowserProfile(
                            agent_id=AGENT_ID,
                            source=SourceCode.ZHAOPIN,
                            profile_key=profile_key,
                            status=status,
                            updated_at=updated_at,
                        )
                    )
                await unit_of_work.commit()

            recorder = AgentHeartbeatRecorder(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=AgentSessionAuthenticator(session_hasher.hash),
                profile_offline_seconds=180,
                clock=lambda: HEARTBEAT_AT,
            )
            result = await recorder.record(
                session_token=SecretStr(SESSION_TOKEN),
                heartbeat=build_heartbeat(),
            )

            assert result.upserted_profiles == 1
            assert result.unavailable_profiles == 1
            agent_row = (
                await connection.execute(
                    select(
                        CollectionAgentRow.status,
                        CollectionAgentRow.heartbeat_at,
                        CollectionAgentRow.capabilities,
                    ).where(CollectionAgentRow.id == AGENT_ID)
                )
            ).one()
            session_last_used = await connection.scalar(
                select(AgentSessionRow.last_used_at).where(
                    AgentSessionRow.agent_id == AGENT_ID
                )
            )
            profile_rows = (
                await connection.execute(
                    select(
                        BrowserProfileRow.profile_key,
                        BrowserProfileRow.status,
                        BrowserProfileRow.updated_at,
                    )
                    .where(BrowserProfileRow.agent_id == AGENT_ID)
                    .order_by(BrowserProfileRow.profile_key)
                )
            ).all()

            assert agent_row.status == "online"
            assert agent_row.heartbeat_at == HEARTBEAT_AT
            assert agent_row.capabilities["supportedSources"] == ["zhaopin"]
            assert session_last_used == HEARTBEAT_AT
            assert [(row.profile_key, row.status) for row in profile_rows] == [
                ("fresh", "busy"),
                ("primary", "ready"),
                ("stale", "unavailable"),
            ]
            assert profile_rows[0].updated_at == HEARTBEAT_AT - timedelta(seconds=30)
            assert profile_rows[1].updated_at == HEARTBEAT_AT
            assert profile_rows[2].updated_at == HEARTBEAT_AT

            await AgentCredentialManager(
                unit_of_work_factory=unit_of_work_factory,
                hash_device_secret=device_hasher.hash,
                clock=lambda: HEARTBEAT_AT + timedelta(seconds=1),
            ).disable(AGENT_ID)

            with pytest.raises(AgentSessionAuthenticationDenied):
                await AgentHeartbeatRecorder(
                    unit_of_work_factory=unit_of_work_factory,
                    authenticator=AgentSessionAuthenticator(session_hasher.hash),
                    profile_offline_seconds=180,
                    clock=lambda: HEARTBEAT_AT + timedelta(seconds=2),
                ).record(
                    session_token=SecretStr(SESSION_TOKEN),
                    heartbeat=build_heartbeat(),
                )

            status_after_rejection = await connection.scalar(
                select(CollectionAgentRow.status).where(CollectionAgentRow.id == AGENT_ID)
            )
            assert status_after_rejection == "disabled"

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
            assert (
                await verification_connection.scalar(
                    select(BrowserProfileRow.id).where(BrowserProfileRow.agent_id == AGENT_ID)
                )
                is None
            )
    finally:
        await engine.dispose()

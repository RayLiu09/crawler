from datetime import UTC, datetime
from uuid import UUID

import pytest
from crawler_api.application.agent_credentials import (
    AgentCredentialManager,
    AgentNotFound,
    DisabledAgentCredentialRotation,
)
from crawler_api.domain.entities import CollectionAgent
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from pydantic import SecretStr

AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NEW_DEVICE_SECRET = "dev_rotated_device_secret"
HASH_KEY = "device-hash-key-that-is-at-least-32-bytes"
CHANGED_AT = datetime(2026, 9, 16, 11, 0, tzinfo=UTC)


class FakeAgentRepository:
    def __init__(self, agent: CollectionAgent | None) -> None:
        self.agent = agent
        self.updated: list[CollectionAgent] = []

    async def get_for_update(self, agent_id: UUID) -> CollectionAgent | None:
        return self.agent if self.agent is not None and self.agent.id == agent_id else None

    async def update(self, agent: CollectionAgent) -> None:
        self.updated.append(agent)


class FakeSessionRepository:
    def __init__(self, revoked_count: int = 2) -> None:
        self.revoked_count = revoked_count
        self.revocations: list[tuple[UUID, datetime]] = []

    async def revoke_active_for_agent(self, agent_id: UUID, revoked_at: datetime) -> int:
        self.revocations.append((agent_id, revoked_at))
        return self.revoked_count


class FakeUnitOfWork:
    def __init__(self, agent: CollectionAgent | None) -> None:
        self.agents = FakeAgentRepository(agent)
        self.agent_sessions = FakeSessionRepository()
        self.committed = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def build_agent(*, status: str = "offline") -> CollectionAgent:
    return CollectionAgent(
        id=AGENT_ID,
        device_credential_hash="hmac-sha256:old",
        display_name="Desktop Agent A",
        status=status,
    )


def build_manager(unit_of_work: FakeUnitOfWork) -> AgentCredentialManager:
    return AgentCredentialManager(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        hash_device_secret=DeviceCredentialHasher(SecretStr(HASH_KEY)).hash,
        clock=lambda: CHANGED_AT,
        device_secret_generator=lambda: NEW_DEVICE_SECRET,
    )


async def test_rotate_changes_hash_and_revokes_sessions_atomically() -> None:
    agent = build_agent()
    unit_of_work = FakeUnitOfWork(agent)

    result = await build_manager(unit_of_work).rotate(AGENT_ID)

    assert result.agent_id == AGENT_ID
    assert result.device_secret == NEW_DEVICE_SECRET
    assert result.revoked_sessions == 2
    assert agent.device_credential_hash == DeviceCredentialHasher(
        SecretStr(HASH_KEY)
    ).hash(NEW_DEVICE_SECRET)
    assert unit_of_work.agents.updated == [agent]
    assert unit_of_work.agent_sessions.revocations == [(AGENT_ID, CHANGED_AT)]
    assert unit_of_work.committed


async def test_disable_is_idempotent_and_revokes_sessions() -> None:
    agent = build_agent(status="disabled")
    unit_of_work = FakeUnitOfWork(agent)

    result = await build_manager(unit_of_work).disable(AGENT_ID)

    assert result.agent_id == AGENT_ID
    assert result.revoked_sessions == 2
    assert agent.status == "disabled"
    assert unit_of_work.agents.updated == [agent]
    assert unit_of_work.agent_sessions.revocations == [(AGENT_ID, CHANGED_AT)]
    assert unit_of_work.committed


async def test_unknown_agent_cannot_be_rotated_or_disabled() -> None:
    for operation in ("rotate", "disable"):
        unit_of_work = FakeUnitOfWork(None)
        manager = build_manager(unit_of_work)

        with pytest.raises(AgentNotFound):
            await getattr(manager, operation)(AGENT_ID)

        assert not unit_of_work.committed


async def test_disabled_agent_credential_cannot_be_rotated() -> None:
    unit_of_work = FakeUnitOfWork(build_agent(status="disabled"))

    with pytest.raises(DisabledAgentCredentialRotation):
        await build_manager(unit_of_work).rotate(AGENT_ID)

    assert unit_of_work.agents.updated == []
    assert unit_of_work.agent_sessions.revocations == []

from uuid import UUID

import pytest
from crawler_api.application.agent_provisioning import (
    AgentAlreadyProvisioned,
    AgentProvisioner,
    InvalidAgentDisplayName,
)
from crawler_api.domain.entities import CollectionAgent
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from pydantic import SecretStr

AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DEVICE_SECRET = "dev_unit_test_device_secret"
HASH_KEY = "device-credential-hash-key-that-is-at-least-32-bytes"


class FakeAgentRepository:
    def __init__(self, agents: list[CollectionAgent] | None = None) -> None:
        self.agents = {agent.id: agent for agent in agents or []}
        self.added: list[CollectionAgent] = []

    async def get(self, agent_id: UUID) -> CollectionAgent | None:
        return self.agents.get(agent_id)

    async def add(self, agent: CollectionAgent) -> None:
        self.agents[agent.id] = agent
        self.added.append(agent)


class FakeUnitOfWork:
    def __init__(self, agents: list[CollectionAgent] | None = None) -> None:
        self.agents = FakeAgentRepository(agents)
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None or not self.committed:
            self.rolled_back = True

    async def commit(self) -> None:
        self.committed = True


def build_provisioner(unit_of_work: FakeUnitOfWork) -> AgentProvisioner:
    hasher = DeviceCredentialHasher(SecretStr(HASH_KEY))
    return AgentProvisioner(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        hash_device_secret=hasher.hash,
        agent_id_generator=lambda: AGENT_ID,
        device_secret_generator=lambda: DEVICE_SECRET,
    )


async def test_provision_creates_offline_agent_with_keyed_device_hash() -> None:
    unit_of_work = FakeUnitOfWork()
    provisioner = build_provisioner(unit_of_work)

    result = await provisioner.provision(display_name="Desktop Agent A")

    assert result.agent_id == AGENT_ID
    assert result.display_name == "Desktop Agent A"
    assert result.device_secret == DEVICE_SECRET
    assert unit_of_work.committed
    assert len(unit_of_work.agents.added) == 1
    agent = unit_of_work.agents.added[0]
    assert agent.id == AGENT_ID
    assert agent.status == "offline"
    assert agent.device_credential_hash == DeviceCredentialHasher(
        SecretStr(HASH_KEY)
    ).hash(DEVICE_SECRET)
    assert agent.device_credential_hash != DEVICE_SECRET
    assert agent.capabilities.supported_sources == []
    assert agent.capabilities.max_concurrent_tasks == 1
    assert agent.capabilities.browser.headful is True


async def test_explicit_agent_id_is_the_retry_idempotency_key() -> None:
    existing = CollectionAgent(
        id=AGENT_ID,
        device_credential_hash="hmac-sha256:existing",
        display_name="Existing Agent",
    )
    unit_of_work = FakeUnitOfWork([existing])
    provisioner = build_provisioner(unit_of_work)

    with pytest.raises(AgentAlreadyProvisioned):
        await provisioner.provision(display_name="Retry Agent", agent_id=AGENT_ID)

    assert unit_of_work.agents.added == []
    assert not unit_of_work.committed
    assert unit_of_work.rolled_back


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 201])
async def test_display_name_is_bounded_before_persistence(display_name: str) -> None:
    unit_of_work = FakeUnitOfWork()
    provisioner = build_provisioner(unit_of_work)

    with pytest.raises(InvalidAgentDisplayName):
        await provisioner.provision(display_name=display_name)

    assert unit_of_work.agents.added == []
    assert not unit_of_work.committed

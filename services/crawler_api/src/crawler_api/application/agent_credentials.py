from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from .agent_provisioning import generate_device_secret
from .ports import UnitOfWork


class AgentNotFound(Exception):
    pass


class DisabledAgentCredentialRotation(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RotatedAgentCredential:
    agent_id: UUID
    device_secret: str
    revoked_sessions: int


@dataclass(frozen=True, slots=True)
class DisabledAgent:
    agent_id: UUID
    revoked_sessions: int


class AgentCredentialManager:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        hash_device_secret: Callable[[str], str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        device_secret_generator: Callable[[], str] = generate_device_secret,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._hash_device_secret = hash_device_secret
        self._clock = clock
        self._device_secret_generator = device_secret_generator

    async def rotate(self, agent_id: UUID) -> RotatedAgentCredential:
        async with self._unit_of_work_factory() as unit_of_work:
            agent = await unit_of_work.agents.get_for_update(agent_id)
            if agent is None:
                raise AgentNotFound
            if agent.status == "disabled":
                raise DisabledAgentCredentialRotation

            device_secret = self._device_secret_generator()
            changed_at = self._clock()
            agent.device_credential_hash = self._hash_device_secret(device_secret)
            await unit_of_work.agents.update(agent)
            revoked_sessions = await unit_of_work.agent_sessions.revoke_active_for_agent(
                agent.id, changed_at
            )
            await unit_of_work.commit()

        return RotatedAgentCredential(
            agent_id=agent_id,
            device_secret=device_secret,
            revoked_sessions=revoked_sessions,
        )

    async def disable(self, agent_id: UUID) -> DisabledAgent:
        async with self._unit_of_work_factory() as unit_of_work:
            agent = await unit_of_work.agents.get_for_update(agent_id)
            if agent is None:
                raise AgentNotFound

            changed_at = self._clock()
            agent.status = "disabled"
            await unit_of_work.agents.update(agent)
            revoked_sessions = await unit_of_work.agent_sessions.revoke_active_for_agent(
                agent.id, changed_at
            )
            await unit_of_work.commit()

        return DisabledAgent(
            agent_id=agent_id,
            revoked_sessions=revoked_sessions,
        )

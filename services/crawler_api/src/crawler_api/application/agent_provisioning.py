import secrets
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from crawler_api.domain.entities import CollectionAgent

from .ports import UnitOfWork


class AgentAlreadyProvisioned(Exception):
    """The requested stable agent identity already exists."""


class InvalidAgentDisplayName(ValueError):
    """The operator-facing Agent name does not satisfy the database contract."""


@dataclass(frozen=True, slots=True)
class ProvisionedAgent:
    agent_id: UUID
    display_name: str
    device_secret: str


def generate_device_secret() -> str:
    return f"dev_{secrets.token_urlsafe(32)}"


class AgentProvisioner:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        hash_device_secret: Callable[[str], str],
        agent_id_generator: Callable[[], UUID] = uuid4,
        device_secret_generator: Callable[[], str] = generate_device_secret,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._hash_device_secret = hash_device_secret
        self._agent_id_generator = agent_id_generator
        self._device_secret_generator = device_secret_generator

    async def provision(
        self,
        *,
        display_name: str,
        agent_id: UUID | None = None,
    ) -> ProvisionedAgent:
        normalized_display_name = display_name.strip()
        if not normalized_display_name or len(normalized_display_name) > 200:
            raise InvalidAgentDisplayName
        resolved_agent_id = agent_id or self._agent_id_generator()

        async with self._unit_of_work_factory() as unit_of_work:
            if await unit_of_work.agents.get(resolved_agent_id) is not None:
                raise AgentAlreadyProvisioned

            device_secret = self._device_secret_generator()
            await unit_of_work.agents.add(
                CollectionAgent(
                    id=resolved_agent_id,
                    device_credential_hash=self._hash_device_secret(device_secret),
                    display_name=normalized_display_name,
                    status="offline",
                )
            )
            await unit_of_work.commit()

        return ProvisionedAgent(
            agent_id=resolved_agent_id,
            display_name=normalized_display_name,
            device_secret=device_secret,
        )

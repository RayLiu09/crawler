import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from crawler_api.domain.entities import AgentSession
from pydantic import SecretStr

from .ports import UnitOfWork

DUMMY_DEVICE_CREDENTIAL_HASH = "hmac-sha256:" + "0" * 64


class AgentAuthenticationDenied(Exception):
    """Uniform failure for unknown, disabled, or incorrectly authenticated Agents."""


@dataclass(frozen=True, slots=True)
class IssuedAgentSession:
    session_token: str
    expires_at: datetime


def generate_agent_session_token() -> str:
    return f"agt_{secrets.token_urlsafe(32)}"


class AgentSessionIssuer:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        verify_device_secret: Callable[[str, str], bool],
        hash_session_token: Callable[[str], str],
        ttl_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_generator: Callable[[], str] = generate_agent_session_token,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._verify_device_secret = verify_device_secret
        self._hash_session_token = hash_session_token
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._token_generator = token_generator

    async def issue(self, agent_id: UUID, device_secret: SecretStr) -> IssuedAgentSession:
        async with self._unit_of_work_factory() as unit_of_work:
            agent = await unit_of_work.agents.get_for_update(agent_id)
            stored_hash = (
                agent.device_credential_hash if agent is not None else DUMMY_DEVICE_CREDENTIAL_HASH
            )
            credentials_match = self._verify_device_secret(
                device_secret.get_secret_value(), stored_hash
            )
            if agent is None or agent.status == "disabled" or not credentials_match:
                raise AgentAuthenticationDenied

            issued_at = self._clock()
            expires_at = issued_at + timedelta(seconds=self._ttl_seconds)
            plaintext_token = self._token_generator()
            await unit_of_work.agent_sessions.add(
                AgentSession(
                    agent_id=agent.id,
                    session_token_hash=self._hash_session_token(plaintext_token),
                    issued_at=issued_at,
                    expires_at=expires_at,
                )
            )
            await unit_of_work.commit()

        return IssuedAgentSession(
            session_token=plaintext_token,
            expires_at=expires_at,
        )

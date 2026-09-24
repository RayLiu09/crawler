from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from crawler_api.domain.entities import AgentSession, CollectionAgent
from pydantic import SecretStr

from .ports import UnitOfWork


class AgentSessionAuthenticationDenied(Exception):
    """Uniform failure for missing, invalid, expired, revoked, or disabled Sessions."""


@dataclass(frozen=True, slots=True)
class AuthenticatedAgent:
    agent: CollectionAgent
    session: AgentSession


class AgentSessionAuthenticator:
    def __init__(self, hash_session_token: Callable[[str], str]) -> None:
        self._hash_session_token = hash_session_token

    async def authenticate(
        self,
        *,
        unit_of_work: UnitOfWork,
        session_token: SecretStr,
        expected_agent_id: UUID,
        authenticated_at: datetime,
    ) -> AuthenticatedAgent:
        token_hash = self._hash_session_token(session_token.get_secret_value())
        initial_session = await unit_of_work.agent_sessions.get_by_token_hash(token_hash)
        if initial_session is None:
            raise AgentSessionAuthenticationDenied

        agent = await unit_of_work.agents.get_for_update(initial_session.agent_id)
        session = await unit_of_work.agent_sessions.get(initial_session.id)
        if (
            agent is None
            or session is None
            or agent.id != expected_agent_id
            or session.agent_id != agent.id
            or session.revoked_at is not None
            or session.expires_at <= authenticated_at
            or agent.status == "disabled"
        ):
            raise AgentSessionAuthenticationDenied

        await unit_of_work.agent_sessions.update_last_used(session.id, authenticated_at)
        session.last_used_at = authenticated_at
        return AuthenticatedAgent(agent=agent, session=session)

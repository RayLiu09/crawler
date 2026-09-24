from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from crawler_api.domain.entities import BrowserProfile
from crawler_contracts.agent_protocol import AgentHeartbeatRequest
from pydantic import SecretStr

from .agent_authentication import AgentSessionAuthenticator
from .ports import UnitOfWork


@dataclass(frozen=True, slots=True)
class RecordedAgentHeartbeat:
    upserted_profiles: int
    unavailable_profiles: int


class AgentHeartbeatRecorder:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: AgentSessionAuthenticator,
        profile_offline_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._profile_offline_seconds = profile_offline_seconds
        self._clock = clock

    async def record(
        self,
        *,
        session_token: SecretStr,
        heartbeat: AgentHeartbeatRequest,
    ) -> RecordedAgentHeartbeat:
        recorded_at = self._clock()
        unavailable_profiles = 0

        async with self._unit_of_work_factory() as unit_of_work:
            authenticated = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                session_token=session_token,
                expected_agent_id=heartbeat.agent_id,
                authenticated_at=recorded_at,
            )
            agent = authenticated.agent
            agent.capabilities = heartbeat.capabilities
            agent.heartbeat_at = recorded_at
            agent.status = "online"
            await unit_of_work.agents.update(agent)

            existing_profiles = await unit_of_work.browser_profiles.list_for_agent(agent.id)
            reported_keys = {
                (profile.source, profile.profile_key) for profile in heartbeat.profiles
            }
            for reported in heartbeat.profiles:
                await unit_of_work.browser_profiles.upsert(
                    BrowserProfile(
                        agent_id=agent.id,
                        source=reported.source,
                        profile_key=reported.profile_key,
                        status=reported.status,
                        updated_at=recorded_at,
                    )
                )

            stale_before = recorded_at - timedelta(seconds=self._profile_offline_seconds)
            for existing in existing_profiles:
                key = (existing.source, existing.profile_key)
                if (
                    key not in reported_keys
                    and existing.status != "unavailable"
                    and existing.updated_at is not None
                    and existing.updated_at <= stale_before
                ):
                    existing.status = "unavailable"
                    existing.updated_at = recorded_at
                    await unit_of_work.browser_profiles.upsert(existing)
                    unavailable_profiles += 1

            await unit_of_work.commit()

        return RecordedAgentHeartbeat(
            upserted_profiles=len(heartbeat.profiles),
            unavailable_profiles=unavailable_profiles,
        )

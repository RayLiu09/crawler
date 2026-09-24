from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.api.v1.agent_heartbeat import router
from crawler_api.application.agent_authentication import (
    AgentSessionAuthenticationDenied,
    AgentSessionAuthenticator,
)
from crawler_api.application.agent_heartbeats import (
    AgentHeartbeatRecorder,
    RecordedAgentHeartbeat,
)
from crawler_api.domain.entities import AgentSession, BrowserProfile, CollectionAgent
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_contracts.agent_protocol import AgentHeartbeatRequest
from crawler_contracts.common import SourceCode
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_AGENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SESSION_TOKEN = "agt_heartbeat_session_token_1234567890"
HASH_KEY = "session-hash-key-that-is-at-least-32-bytes"
RECORDED_AT = datetime(2026, 9, 17, 8, 0, tzinfo=UTC)


class FakeAgentRepository:
    def __init__(self, agent: CollectionAgent | None) -> None:
        self.agent = agent
        self.locked_ids: list[UUID] = []
        self.updated: list[CollectionAgent] = []

    async def get_for_update(self, agent_id: UUID) -> CollectionAgent | None:
        self.locked_ids.append(agent_id)
        return self.agent if self.agent is not None and self.agent.id == agent_id else None

    async def update(self, agent: CollectionAgent) -> None:
        self.updated.append(agent)


class FakeAgentSessionRepository:
    def __init__(
        self,
        initial_session: AgentSession | None,
        locked_session: AgentSession | None = None,
    ) -> None:
        self.initial_session = initial_session
        self.locked_session = locked_session or initial_session
        self.last_used_updates: list[tuple[UUID, datetime]] = []

    async def get_by_token_hash(self, token_hash: str) -> AgentSession | None:
        if self.initial_session is None:
            return None
        return (
            self.initial_session
            if self.initial_session.session_token_hash == token_hash
            else None
        )

    async def get(self, session_id: UUID) -> AgentSession | None:
        if self.locked_session is None or self.locked_session.id != session_id:
            return None
        return self.locked_session

    async def update_last_used(self, session_id: UUID, last_used_at: datetime) -> None:
        self.last_used_updates.append((session_id, last_used_at))


class FakeBrowserProfileRepository:
    def __init__(self, profiles: list[BrowserProfile]) -> None:
        self.profiles = profiles
        self.upserted: list[BrowserProfile] = []

    async def list_for_agent(self, agent_id: UUID) -> list[BrowserProfile]:
        return [profile for profile in self.profiles if profile.agent_id == agent_id]

    async def upsert(self, profile: BrowserProfile) -> None:
        self.upserted.append(profile)


class FakeUnitOfWork:
    def __init__(
        self,
        *,
        agent: CollectionAgent | None,
        initial_session: AgentSession | None,
        locked_session: AgentSession | None = None,
        profiles: list[BrowserProfile] | None = None,
    ) -> None:
        self.agents = FakeAgentRepository(agent)
        self.agent_sessions = FakeAgentSessionRepository(initial_session, locked_session)
        self.browser_profiles = FakeBrowserProfileRepository(profiles or [])
        self.committed = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def session_token_hash() -> str:
    return AgentSessionTokenHasher(SecretStr(HASH_KEY)).hash(SESSION_TOKEN)


def build_agent(*, status: str = "offline") -> CollectionAgent:
    return CollectionAgent(
        id=AGENT_ID,
        device_credential_hash="hmac-sha256:device",
        display_name="Desktop Agent A",
        status=status,
    )


def build_session(
    *,
    agent_id: UUID = AGENT_ID,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> AgentSession:
    return AgentSession(
        agent_id=agent_id,
        session_token_hash=session_token_hash(),
        issued_at=RECORDED_AT - timedelta(minutes=1),
        expires_at=expires_at or RECORDED_AT + timedelta(minutes=10),
        revoked_at=revoked_at,
    )


def build_heartbeat(*, agent_id: UUID = AGENT_ID) -> AgentHeartbeatRequest:
    return AgentHeartbeatRequest.model_validate(
        {
            "agentId": str(agent_id),
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


def build_recorder(unit_of_work: FakeUnitOfWork) -> AgentHeartbeatRecorder:
    token_hasher = AgentSessionTokenHasher(SecretStr(HASH_KEY))
    return AgentHeartbeatRecorder(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=AgentSessionAuthenticator(token_hasher.hash),
        profile_offline_seconds=180,
        clock=lambda: RECORDED_AT,
    )


async def test_authenticated_heartbeat_updates_agent_session_and_profiles_atomically() -> None:
    stale_at = RECORDED_AT - timedelta(seconds=181)
    fresh_at = RECORDED_AT - timedelta(seconds=30)
    reported_existing = BrowserProfile(
        agent_id=AGENT_ID,
        source=SourceCode.ZHAOPIN,
        profile_key="primary",
        status="login_required",
        updated_at=stale_at,
    )
    stale_unreported = BrowserProfile(
        agent_id=AGENT_ID,
        source=SourceCode.ZHAOPIN,
        profile_key="secondary",
        status="ready",
        updated_at=stale_at,
    )
    fresh_unreported = BrowserProfile(
        agent_id=AGENT_ID,
        source=SourceCode.ZHAOPIN,
        profile_key="fresh",
        status="ready",
        updated_at=fresh_at,
    )
    already_unavailable = BrowserProfile(
        agent_id=AGENT_ID,
        source=SourceCode.ZHAOPIN,
        profile_key="unavailable",
        status="unavailable",
        updated_at=stale_at,
    )
    session = build_session()
    agent = build_agent()
    unit_of_work = FakeUnitOfWork(
        agent=agent,
        initial_session=session,
        profiles=[
            reported_existing,
            stale_unreported,
            fresh_unreported,
            already_unavailable,
        ],
    )

    result = await build_recorder(unit_of_work).record(
        session_token=SecretStr(SESSION_TOKEN),
        heartbeat=build_heartbeat(),
    )

    assert result == RecordedAgentHeartbeat(upserted_profiles=1, unavailable_profiles=1)
    assert unit_of_work.committed
    assert agent.status == "online"
    assert agent.heartbeat_at == RECORDED_AT
    assert agent.capabilities.supported_sources == [SourceCode.ZHAOPIN]
    assert unit_of_work.agents.updated == [agent]
    assert unit_of_work.agent_sessions.last_used_updates == [(session.id, RECORDED_AT)]
    assert session.last_used_at == RECORDED_AT
    assert [
        (profile.profile_key, profile.status)
        for profile in unit_of_work.browser_profiles.upserted
    ] == [("primary", "ready"), ("secondary", "unavailable")]
    assert fresh_unreported.status == "ready"
    assert already_unavailable.status == "unavailable"


@pytest.mark.parametrize(
    ("agent", "initial_session", "locked_session", "heartbeat_agent_id"),
    [
        (build_agent(), None, None, AGENT_ID),
        (build_agent(status="disabled"), build_session(), None, AGENT_ID),
        (
            build_agent(),
            build_session(),
            build_session(revoked_at=RECORDED_AT - timedelta(seconds=1)),
            AGENT_ID,
        ),
        (
            build_agent(),
            build_session(),
            build_session(expires_at=RECORDED_AT),
            AGENT_ID,
        ),
        (build_agent(), build_session(), None, OTHER_AGENT_ID),
    ],
)
async def test_invalid_session_states_cannot_write_heartbeat(
    agent: CollectionAgent | None,
    initial_session: AgentSession | None,
    locked_session: AgentSession | None,
    heartbeat_agent_id: UUID,
) -> None:
    unit_of_work = FakeUnitOfWork(
        agent=agent,
        initial_session=initial_session,
        locked_session=locked_session,
    )

    with pytest.raises(AgentSessionAuthenticationDenied):
        await build_recorder(unit_of_work).record(
            session_token=SecretStr(SESSION_TOKEN),
            heartbeat=build_heartbeat(agent_id=heartbeat_agent_id),
        )

    assert unit_of_work.agents.updated == []
    assert unit_of_work.browser_profiles.upserted == []
    assert not unit_of_work.committed


class StubHeartbeatRecorder:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[SecretStr, AgentHeartbeatRequest]] = []

    async def record(
        self,
        *,
        session_token: SecretStr,
        heartbeat: AgentHeartbeatRequest,
    ) -> RecordedAgentHeartbeat:
        if self.failure is not None:
            raise self.failure
        self.calls.append((session_token, heartbeat))
        return RecordedAgentHeartbeat(upserted_profiles=1, unavailable_profiles=0)


def build_route_app(recorder: StubHeartbeatRecorder) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.agent_heartbeat_recorder = recorder
    return app


async def test_heartbeat_route_requires_bearer_and_returns_204() -> None:
    recorder = StubHeartbeatRecorder()
    app = build_route_app(recorder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
            json=build_heartbeat().model_dump(mode="json", by_alias=True),
        )

    assert response.status_code == 204
    assert response.content == b""
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0].get_secret_value() == SESSION_TOKEN


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": f"Basic {SESSION_TOKEN}"}],
)
async def test_heartbeat_route_rejects_missing_or_wrong_auth_scheme(
    headers: dict[str, str],
) -> None:
    recorder = StubHeartbeatRecorder()
    app = build_route_app(recorder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/heartbeat",
            headers=headers,
            json=build_heartbeat().model_dump(mode="json", by_alias=True),
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid agent session"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert recorder.calls == []


async def test_heartbeat_route_maps_session_failure_to_uniform_401() -> None:
    recorder = StubHeartbeatRecorder(AgentSessionAuthenticationDenied())
    app = build_route_app(recorder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/heartbeat",
            headers={"Authorization": f"Bearer {SESSION_TOKEN}"},
            json=build_heartbeat().model_dump(mode="json", by_alias=True),
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid agent session"}

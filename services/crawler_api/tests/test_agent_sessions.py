from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.api.v1.agent_sessions import router
from crawler_api.application.agent_sessions import (
    DUMMY_DEVICE_CREDENTIAL_HASH,
    AgentAuthenticationDenied,
    AgentSessionIssuer,
    IssuedAgentSession,
)
from crawler_api.domain.entities import AgentSession, CollectionAgent
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher
from crawler_contracts.agent_protocol import AgentSessionResponse
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DEVICE_SECRET = "dev_unit_test_agent_device_secret"
SESSION_TOKEN = "agt_unit_test_session_token_1234567890"
DEVICE_HASH_KEY = "device-hash-key-that-is-at-least-32-bytes"
SESSION_HASH_KEY = "session-hash-key-that-is-at-least-32-bytes"
ISSUED_AT = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


class FakeAgentRepository:
    def __init__(self, agent: CollectionAgent | None) -> None:
        self.agent = agent
        self.locked_ids: list[UUID] = []

    async def get_for_update(self, agent_id: UUID) -> CollectionAgent | None:
        self.locked_ids.append(agent_id)
        return self.agent if self.agent is not None and self.agent.id == agent_id else None


class FakeAgentSessionRepository:
    def __init__(self) -> None:
        self.added: list[AgentSession] = []

    async def add(self, session: AgentSession) -> None:
        self.added.append(session)


class FakeUnitOfWork:
    def __init__(self, agent: CollectionAgent | None) -> None:
        self.agents = FakeAgentRepository(agent)
        self.agent_sessions = FakeAgentSessionRepository()
        self.committed = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def build_agent(*, status: str = "offline") -> CollectionAgent:
    device_hasher = DeviceCredentialHasher(SecretStr(DEVICE_HASH_KEY))
    return CollectionAgent(
        id=AGENT_ID,
        device_credential_hash=device_hasher.hash(DEVICE_SECRET),
        display_name="Desktop Agent A",
        status=status,
    )


def build_issuer(
    unit_of_work: FakeUnitOfWork,
    *,
    verify_device_secret: object | None = None,
) -> AgentSessionIssuer:
    device_hasher = DeviceCredentialHasher(SecretStr(DEVICE_HASH_KEY))
    session_hasher = AgentSessionTokenHasher(SecretStr(SESSION_HASH_KEY))
    verifier = (
        device_hasher.verify
        if verify_device_secret is None
        else verify_device_secret
    )
    return AgentSessionIssuer(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        verify_device_secret=verifier,  # type: ignore[arg-type]
        hash_session_token=session_hasher.hash,
        ttl_seconds=900,
        clock=lambda: ISSUED_AT,
        token_generator=lambda: SESSION_TOKEN,
    )


async def test_valid_device_credential_issues_hashed_short_lived_session() -> None:
    unit_of_work = FakeUnitOfWork(build_agent())
    issuer = build_issuer(unit_of_work)

    result = await issuer.issue(AGENT_ID, SecretStr(DEVICE_SECRET))

    assert result == IssuedAgentSession(
        session_token=SESSION_TOKEN,
        expires_at=ISSUED_AT + timedelta(seconds=900),
    )
    assert unit_of_work.agents.locked_ids == [AGENT_ID]
    assert unit_of_work.committed
    assert len(unit_of_work.agent_sessions.added) == 1
    stored = unit_of_work.agent_sessions.added[0]
    assert stored.agent_id == AGENT_ID
    assert stored.session_token_hash == AgentSessionTokenHasher(
        SecretStr(SESSION_HASH_KEY)
    ).hash(SESSION_TOKEN)
    assert stored.session_token_hash != SESSION_TOKEN
    assert stored.issued_at == ISSUED_AT
    assert stored.expires_at == ISSUED_AT + timedelta(seconds=900)


@pytest.mark.parametrize(
    ("agent", "device_secret"),
    [
        (None, DEVICE_SECRET),
        (build_agent(status="disabled"), DEVICE_SECRET),
        (build_agent(), "incorrect-device-secret"),
    ],
)
async def test_unknown_disabled_and_wrong_secret_share_authentication_failure(
    agent: CollectionAgent | None,
    device_secret: str,
) -> None:
    unit_of_work = FakeUnitOfWork(agent)
    issuer = build_issuer(unit_of_work)

    with pytest.raises(AgentAuthenticationDenied):
        await issuer.issue(AGENT_ID, SecretStr(device_secret))

    assert unit_of_work.agent_sessions.added == []
    assert not unit_of_work.committed


async def test_unknown_agent_still_executes_dummy_credential_comparison() -> None:
    comparisons: list[tuple[str, str]] = []

    def verify(device_secret: str, stored_hash: str) -> bool:
        comparisons.append((device_secret, stored_hash))
        return False

    issuer = build_issuer(
        FakeUnitOfWork(None),
        verify_device_secret=verify,
    )

    with pytest.raises(AgentAuthenticationDenied):
        await issuer.issue(AGENT_ID, SecretStr(DEVICE_SECRET))

    assert comparisons == [(DEVICE_SECRET, DUMMY_DEVICE_CREDENTIAL_HASH)]


class StubAgentSessionIssuer:
    def __init__(self, result: IssuedAgentSession | Exception) -> None:
        self._result = result

    async def issue(self, agent_id: UUID, device_secret: SecretStr) -> IssuedAgentSession:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def build_route_app(result: IssuedAgentSession | Exception) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.agent_session_issuer = StubAgentSessionIssuer(result)
    return app


async def test_agent_session_route_uses_existing_contract() -> None:
    app = build_route_app(
        IssuedAgentSession(
            session_token=SESSION_TOKEN,
            expires_at=ISSUED_AT + timedelta(seconds=900),
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/sessions",
            json={"agentId": str(AGENT_ID), "deviceSecret": DEVICE_SECRET},
        )

    assert response.status_code == 200
    assert response.json() == AgentSessionResponse(
        session_token=SESSION_TOKEN,
        expires_at=ISSUED_AT + timedelta(seconds=900),
    ).model_dump(mode="json", by_alias=True)


async def test_agent_session_route_returns_uniform_401() -> None:
    app = build_route_app(AgentAuthenticationDenied())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/sessions",
            json={"agentId": str(AGENT_ID), "deviceSecret": "invalid"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid agent credentials"}
    assert response.headers["www-authenticate"] == "Bearer"

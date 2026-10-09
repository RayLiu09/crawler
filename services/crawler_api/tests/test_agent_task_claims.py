from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from crawler_api.api.v1.agent_tasks import router
from crawler_api.application.agent_authentication import (
    AgentSessionAuthenticationDenied,
    AgentSessionAuthenticator,
)
from crawler_api.application.agent_task_claims import (
    AgentTaskClaimer,
    AgentTaskClaimUnavailable,
    ClaimRequestConflict,
)
from crawler_api.domain.entities import (
    AgentSession,
    BrowserProfile,
    CollectionAgent,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.enums import CollectionState
from crawler_api.domain.task_attempts import CollectionTaskAttempt
from crawler_api.infrastructure.database.repositories import (
    SqlAlchemyCollectionJobRepository,
    SqlAlchemyCollectionTaskRepository,
)
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_api.infrastructure.security.task_lease_tokens import TaskLeaseTokenManager
from crawler_contracts.agent_protocol import (
    AgentCapabilities,
    ClaimTaskRequest,
    TaskLease,
)
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import SourceCode
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_AGENT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CLIENT_ID = UUID("11111111-1111-4111-8111-111111111111")
SESSION_ID = UUID("22222222-2222-4222-8222-222222222222")
CLAIM_REQUEST_ID = UUID("33333333-3333-4333-8333-333333333333")
SESSION_TOKEN = "agt_claim_session_token_1234567890"
HASH_KEY = "agent-token-hash-key-that-is-at-least-32-bytes"
CLAIMED_AT = datetime(2026, 9, 17, 11, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeAgentRepository:
    def __init__(self, agent: CollectionAgent) -> None:
        self.agent = agent
        self.updated: list[CollectionAgent] = []

    async def get_for_update(self, agent_id: UUID) -> CollectionAgent | None:
        return self.agent if self.agent.id == agent_id else None

    async def update(self, agent: CollectionAgent) -> None:
        self.updated.append(agent)


class FakeAgentSessionRepository:
    def __init__(self, session: AgentSession) -> None:
        self.session = session
        self.last_used_updates: list[tuple[UUID, datetime]] = []

    async def get_by_token_hash(self, token_hash: str) -> AgentSession | None:
        return self.session if self.session.session_token_hash == token_hash else None

    async def get(self, session_id: UUID) -> AgentSession | None:
        return self.session if self.session.id == session_id else None

    async def update_last_used(self, session_id: UUID, used_at: datetime) -> None:
        self.last_used_updates.append((session_id, used_at))


class FakeBrowserProfileRepository:
    def __init__(self, profiles: list[BrowserProfile]) -> None:
        self.profiles = profiles
        self.upserted: list[BrowserProfile] = []

    async def get(self, profile_id: UUID) -> BrowserProfile | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    async def list_for_agent(self, agent_id: UUID) -> list[BrowserProfile]:
        return [profile for profile in self.profiles if profile.agent_id == agent_id]

    async def upsert(self, profile: BrowserProfile) -> None:
        self.upserted.append(profile)


class FakeCollectionJobRepository:
    def __init__(self, job: CollectionJob | None) -> None:
        self.job = job
        self.runtime_updates: list[CollectionJob] = []

    async def lock_next_claimable(
        self, sources: list[SourceCode]
    ) -> CollectionJob | None:
        if self.job is None or self.job.request_payload.source not in sources:
            return None
        return self.job

    async def get_for_update(self, job_id: UUID) -> CollectionJob | None:
        if self.job is not None and self.job.id == job_id:
            return self.job
        return None

    async def update_runtime_state(
        self, job: CollectionJob, updated_at: datetime
    ) -> None:
        assert updated_at == CLAIMED_AT
        self.runtime_updates.append(job)


class FakeCollectionTaskRepository:
    def __init__(self, task: CollectionTask | None) -> None:
        self.task = task
        self.lease_updates: list[CollectionTask] = []

    async def lock_next_claimable(
        self, job_id: UUID, sources: list[SourceCode]
    ) -> CollectionTask | None:
        if (
            self.task is None
            or self.task.collection_job_id != job_id
            or self.task.source not in sources
        ):
            return None
        return self.task

    async def get(self, task_id: UUID) -> CollectionTask | None:
        return self.task if self.task is not None and self.task.id == task_id else None

    async def get_for_update(self, task_id: UUID) -> CollectionTask | None:
        return await self.get(task_id)

    async def update_lease_state(
        self, task: CollectionTask, updated_at: datetime
    ) -> None:
        assert updated_at == CLAIMED_AT
        self.lease_updates.append(task)


class FakeTaskAttemptRepository:
    def __init__(self) -> None:
        self.attempts: dict[UUID, CollectionTaskAttempt] = {}
        self.add_count = 0

    async def get(self, attempt_id: UUID) -> CollectionTaskAttempt | None:
        return self.attempts.get(attempt_id)

    async def add(self, attempt: CollectionTaskAttempt) -> None:
        self.attempts[attempt.id] = attempt
        self.add_count += 1


class FakeUnitOfWork:
    def __init__(
        self,
        *,
        agent: CollectionAgent,
        session: AgentSession,
        profiles: list[BrowserProfile],
        job: CollectionJob | None,
        task: CollectionTask | None,
    ) -> None:
        self.agents = FakeAgentRepository(agent)
        self.agent_sessions = FakeAgentSessionRepository(session)
        self.browser_profiles = FakeBrowserProfileRepository(profiles)
        self.collection_jobs = FakeCollectionJobRepository(job)
        self.collection_tasks = FakeCollectionTaskRepository(task)
        self.task_attempts = FakeTaskAttemptRepository()
        self.commit_count = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


def token_hasher() -> AgentSessionTokenHasher:
    return AgentSessionTokenHasher(SecretStr(HASH_KEY))


def lease_tokens() -> TaskLeaseTokenManager:
    return TaskLeaseTokenManager(SecretStr(HASH_KEY))


def build_agent(
    *,
    status: str = "online",
    heartbeat_at: datetime | None = CLAIMED_AT,
    current_task_id: UUID | None = None,
    supported_sources: list[SourceCode] | None = None,
) -> CollectionAgent:
    return CollectionAgent(
        id=AGENT_ID,
        device_credential_hash="hmac-sha256:" + "a" * 64,
        display_name="Claim Agent",
        status=status,
        heartbeat_at=heartbeat_at,
        current_task_id=current_task_id,
        capabilities=AgentCapabilities(
            supported_sources=supported_sources
            if supported_sources is not None
            else [SourceCode.ZHAOPIN]
        ),
    )


def build_session() -> AgentSession:
    return AgentSession(
        id=SESSION_ID,
        agent_id=AGENT_ID,
        session_token_hash=token_hasher().hash(SESSION_TOKEN),
        issued_at=CLAIMED_AT - timedelta(minutes=1),
        expires_at=CLAIMED_AT + timedelta(minutes=10),
    )


def build_profile(
    *,
    profile_key: str = "primary",
    status: str = "ready",
    updated_at: datetime | None = CLAIMED_AT,
) -> BrowserProfile:
    return BrowserProfile(
        agent_id=AGENT_ID,
        source=SourceCode.ZHAOPIN,
        profile_key=profile_key,
        status=status,
        updated_at=updated_at,
    )


def build_job() -> CollectionJob:
    return CollectionJob(
        client_id=CLIENT_ID,
        external_request_id="jobreq_claim_001",
        idempotency_key_hash="sha256:" + "b" * 64,
        request_payload=CollectionJobPayload(
            source=SourceCode.ZHAOPIN,
            queries=[{"keyword": "Python", "region": "Beijing"}],
        ),
        created_at=CLAIMED_AT - timedelta(minutes=2),
        updated_at=CLAIMED_AT - timedelta(minutes=2),
    )


def build_task(job: CollectionJob) -> CollectionTask:
    return CollectionTask(
        collection_job_id=job.id,
        source=SourceCode.ZHAOPIN,
        keyword="Python",
        region="Beijing",
        page_limit=10,
        created_at=CLAIMED_AT - timedelta(minutes=1),
        updated_at=CLAIMED_AT - timedelta(minutes=1),
    )


def build_unit_of_work(
    *,
    agent: CollectionAgent | None = None,
    profiles: list[BrowserProfile] | None = None,
    with_task: bool = True,
) -> FakeUnitOfWork:
    job = build_job() if with_task else None
    task = build_task(job) if job is not None else None
    return FakeUnitOfWork(
        agent=agent or build_agent(),
        session=build_session(),
        profiles=profiles if profiles is not None else [build_profile()],
        job=job,
        task=task,
    )


def build_claimer(
    unit_of_work: FakeUnitOfWork,
    *,
    clock: MutableClock | None = None,
) -> AgentTaskClaimer:
    return AgentTaskClaimer(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=AgentSessionAuthenticator(token_hasher().hash),
        lease_tokens=lease_tokens(),
        lease_seconds=120,
        agent_offline_seconds=180,
        clock=clock or MutableClock(CLAIMED_AT),
    )


def claim_request() -> ClaimTaskRequest:
    return ClaimTaskRequest(
        agent_id=AGENT_ID,
        claim_request_id=CLAIM_REQUEST_ID,
    )


async def claim(claimer: AgentTaskClaimer) -> TaskLease | None:
    return await claimer.claim(
        session_token=SecretStr(SESSION_TOKEN),
        request=claim_request(),
    )


async def test_claim_assigns_task_profile_lease_and_attempt_atomically() -> None:
    unit_of_work = build_unit_of_work(
        profiles=[build_profile(profile_key="primary"), build_profile(profile_key="secondary")]
    )

    lease = await claim(build_claimer(unit_of_work))

    assert lease is not None
    task = unit_of_work.collection_tasks.task
    job = unit_of_work.collection_jobs.job
    agent = unit_of_work.agents.agent
    assert task is not None
    assert job is not None
    assert lease.attempt_id == CLAIM_REQUEST_ID
    assert lease.profile_key == "primary"
    assert lease.lease_token.startswith("lse_")
    assert lease.lease_expires_at == CLAIMED_AT + timedelta(seconds=120)
    assert task.status is CollectionState.RUNNING
    assert task.leased_agent_id == AGENT_ID
    assert task.leased_profile_id == unit_of_work.browser_profiles.profiles[0].id
    assert task.lease_token_hash == lease_tokens().hash(lease.lease_token)
    assert lease.lease_token not in task.lease_token_hash
    assert task.fencing_token == 1
    assert task.attempt_count == 1
    assert unit_of_work.browser_profiles.profiles[0].status == "busy"
    assert unit_of_work.browser_profiles.profiles[1].status == "ready"
    assert agent.current_task_id == task.id
    assert job.status is CollectionState.RUNNING
    assert job.version == 2
    attempt = unit_of_work.task_attempts.attempts[CLAIM_REQUEST_ID]
    assert attempt.collection_task_id == task.id
    assert attempt.agent_id == AGENT_ID
    assert attempt.fencing_token == 1
    assert attempt.claimed_at == CLAIMED_AT
    assert unit_of_work.agent_sessions.last_used_updates == [(SESSION_ID, CLAIMED_AT)]
    assert unit_of_work.commit_count == 1


async def test_claim_exposes_authoritative_resume_position() -> None:
    unit_of_work = build_unit_of_work()
    task = unit_of_work.collection_tasks.task
    assert task is not None
    task.page_checkpoint = 5
    task.processed_count = 28

    lease = await claim(build_claimer(unit_of_work))

    assert lease is not None
    assert (lease.page_checkpoint, lease.processed_count) == (5, 28)


async def test_same_claim_request_replays_identical_active_lease() -> None:
    unit_of_work = build_unit_of_work()
    claimer = build_claimer(unit_of_work)

    first = await claim(claimer)
    replay = await claim(claimer)

    assert first == replay
    assert unit_of_work.task_attempts.add_count == 1
    assert len(unit_of_work.collection_tasks.lease_updates) == 1
    assert unit_of_work.collection_tasks.task is not None
    assert unit_of_work.collection_tasks.task.fencing_token == 1
    assert unit_of_work.commit_count == 2


async def test_no_matching_task_returns_none_but_commits_session_use() -> None:
    unit_of_work = build_unit_of_work(with_task=False)

    result = await claim(build_claimer(unit_of_work))

    assert result is None
    assert unit_of_work.task_attempts.attempts == {}
    assert unit_of_work.commit_count == 1
    assert unit_of_work.agent_sessions.last_used_updates == [(SESSION_ID, CLAIMED_AT)]


@pytest.mark.parametrize(
    ("agent", "profiles"),
    [
        (build_agent(status="offline"), [build_profile()]),
        (
            build_agent(heartbeat_at=CLAIMED_AT - timedelta(seconds=181)),
            [build_profile()],
        ),
        (build_agent(current_task_id=UUID(int=9)), [build_profile()]),
        (build_agent(supported_sources=[]), [build_profile()]),
        (build_agent(), [build_profile(status="login_required")]),
        (
            build_agent(),
            [build_profile(updated_at=CLAIMED_AT - timedelta(seconds=181))],
        ),
    ],
)
async def test_unready_agent_or_profile_cannot_claim(
    agent: CollectionAgent,
    profiles: list[BrowserProfile],
) -> None:
    unit_of_work = build_unit_of_work(agent=agent, profiles=profiles)

    with pytest.raises(AgentTaskClaimUnavailable):
        await claim(build_claimer(unit_of_work))

    assert unit_of_work.task_attempts.attempts == {}
    assert unit_of_work.commit_count == 0


async def test_expired_claim_request_cannot_be_reused() -> None:
    unit_of_work = build_unit_of_work()
    clock = MutableClock(CLAIMED_AT)
    claimer = build_claimer(unit_of_work, clock=clock)
    assert await claim(claimer) is not None
    clock.now = CLAIMED_AT + timedelta(seconds=121)

    with pytest.raises(ClaimRequestConflict):
        await claim(claimer)

    assert unit_of_work.task_attempts.add_count == 1


class CapturingSqlSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> None:
        self.statements.append(statement)


async def test_claim_candidate_queries_compile_with_skip_locked() -> None:
    session = CapturingSqlSession()
    sqlalchemy_session = cast(AsyncSession, session)

    assert (
        await SqlAlchemyCollectionJobRepository(sqlalchemy_session).lock_next_claimable(
            [SourceCode.ZHAOPIN]
        )
        is None
    )
    assert (
        await SqlAlchemyCollectionTaskRepository(sqlalchemy_session).lock_next_claimable(
            UUID(int=7), [SourceCode.ZHAOPIN]
        )
        is None
    )

    compiled = [
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for statement in session.statements
    ]
    assert len(compiled) == 2
    assert all("FOR UPDATE SKIP LOCKED" in sql for sql in compiled)


class StubClaimer:
    def __init__(self, result: TaskLease | None | Exception) -> None:
        self.result = result

    async def claim(self, **kwargs: object) -> TaskLease | None:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def route_lease() -> TaskLease:
    return TaskLease(
        agent_id=AGENT_ID,
        task_id=UUID("44444444-4444-4444-8444-444444444444"),
        attempt_id=CLAIM_REQUEST_ID,
        source=SourceCode.ZHAOPIN,
        profile_key="primary",
        keyword="Python",
        region="Beijing",
        page_limit=10,
        page_checkpoint=0,
        processed_count=0,
        lease_token="lse_abcdefghijklmnopqrstuvwxyz1234567890ABCDEFG",
        fencing_token=1,
        lease_expires_at=CLAIMED_AT + timedelta(seconds=120),
    )


def build_route_app(result: TaskLease | None | Exception) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.agent_task_claimer = StubClaimer(result)
    return app


async def post_claim(
    app: FastAPI, *, with_token: bool = True
) -> Response:
    headers = {"Authorization": f"Bearer {SESSION_TOKEN}"} if with_token else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/v1/agent/tasks/claim",
            headers=headers,
            json=claim_request().model_dump(mode="json", by_alias=True),
        )


async def test_claim_route_returns_lease_or_no_content() -> None:
    lease = route_lease()

    claimed = await post_claim(build_route_app(lease))
    empty = await post_claim(build_route_app(None))

    assert claimed.status_code == 200
    assert claimed.json()["attemptId"] == str(CLAIM_REQUEST_ID)
    assert claimed.json()["profileKey"] == "primary"
    assert claimed.json()["processedCount"] == 0
    assert empty.status_code == 204
    assert empty.content == b""


@pytest.mark.parametrize(
    ("result", "with_token", "expected_status"),
    [
        (route_lease(), False, 401),
        (AgentSessionAuthenticationDenied(), True, 401),
        (AgentTaskClaimUnavailable(), True, 409),
        (ClaimRequestConflict(), True, 409),
    ],
)
async def test_claim_route_maps_authentication_and_conflicts(
    result: TaskLease | Exception,
    with_token: bool,
    expected_status: int,
) -> None:
    response = await post_claim(build_route_app(result), with_token=with_token)

    assert response.status_code == expected_status

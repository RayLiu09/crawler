from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from crawler_api.api.v1.agent_tasks import router
from crawler_api.application.agent_authentication import AgentSessionAuthenticator
from crawler_api.application.agent_task_runtime import (
    AgentTaskRuntime,
    ExpiredLeaseRecoverer,
    TaskRuntimeStateConflict,
)
from crawler_api.application.task_leases import (
    ActiveTaskLeaseAuthenticator,
    TaskLeaseStateConflict,
)
from crawler_api.domain.callbacks import CollectionJobCallbackConfig
from crawler_api.domain.entities import (
    AgentSession,
    BrowserProfile,
    CollectionAgent,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.enums import CollectionState, DesiredState
from crawler_api.domain.events import StoredCollectionEvent
from crawler_api.domain.task_attempts import (
    CollectionTaskAttempt,
    TaskAttemptOutcome,
)
from crawler_api.domain.webhooks import WebhookOutboxEntry
from crawler_api.infrastructure.security.agent_session_tokens import AgentSessionTokenHasher
from crawler_api.infrastructure.security.task_lease_tokens import TaskLeaseTokenManager
from crawler_contracts.agent_protocol import (
    AgentCapabilities,
    CompleteTaskRequest,
    LeaseReference,
    ReleaseTaskRequest,
    TaskCheckpointRequest,
)
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import SourceCode
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

NOW = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)
AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SESSION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ATTEMPT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
CLIENT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
SESSION_TOKEN = "agt_runtime_session_token_123456789"
HASH_KEY = "runtime-test-hash-key-that-is-at-least-32-bytes"


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class AgentRepository:
    def __init__(self, agent: CollectionAgent) -> None:
        self.agent = agent
        self.clear_count = 0

    async def get_for_update(self, agent_id: UUID) -> CollectionAgent | None:
        return self.agent if self.agent.id == agent_id else None

    async def clear_current_task(
        self, agent_id: UUID, task_id: UUID, updated_at: datetime
    ) -> bool:
        if self.agent.id != agent_id or self.agent.current_task_id != task_id:
            return False
        self.agent.current_task_id = None
        self.agent.updated_at = updated_at
        self.clear_count += 1
        return True


class SessionRepository:
    def __init__(self, session: AgentSession) -> None:
        self.session = session

    async def get_by_token_hash(self, token_hash: str) -> AgentSession | None:
        return self.session if self.session.session_token_hash == token_hash else None

    async def get(self, session_id: UUID) -> AgentSession | None:
        return self.session if self.session.id == session_id else None

    async def update_last_used(self, session_id: UUID, last_used_at: datetime) -> None:
        if self.session.id == session_id:
            self.session.last_used_at = last_used_at


class ProfileRepository:
    def __init__(self, profile: BrowserProfile) -> None:
        self.profile = profile
        self.upsert_count = 0

    async def get(self, profile_id: UUID) -> BrowserProfile | None:
        return self.profile if self.profile.id == profile_id else None

    async def upsert(self, profile: BrowserProfile) -> None:
        self.profile = profile
        self.upsert_count += 1


class JobRepository:
    def __init__(self, job: CollectionJob) -> None:
        self.job = job
        self.update_count = 0

    async def get_for_update(self, job_id: UUID) -> CollectionJob | None:
        return self.job if self.job.id == job_id else None

    async def find_next_with_expired_lease(
        self, expired_at: datetime
    ) -> CollectionJob | None:
        return self.job

    async def update_runtime_state(
        self, job: CollectionJob, updated_at: datetime
    ) -> None:
        self.job = job
        self.update_count += 1


class TaskRepository:
    def __init__(self, task: CollectionTask) -> None:
        self.task = task
        self.update_count = 0

    async def get(self, task_id: UUID) -> CollectionTask | None:
        return self.task if self.task.id == task_id else None

    async def get_for_update(self, task_id: UUID) -> CollectionTask | None:
        return await self.get(task_id)

    async def find_expired_for_job(
        self, job_id: UUID, expired_at: datetime
    ) -> CollectionTask | None:
        if (
            self.task.collection_job_id == job_id
            and self.task.lease_expires_at is not None
            and self.task.lease_expires_at <= expired_at
        ):
            return self.task
        return None

    async def list_for_job_for_update(self, job_id: UUID) -> list[CollectionTask]:
        return [self.task] if self.task.collection_job_id == job_id else []

    async def update_runtime_state(
        self, task: CollectionTask, updated_at: datetime
    ) -> None:
        self.task = task
        self.task.updated_at = updated_at
        self.update_count += 1


class AttemptRepository:
    def __init__(self, attempt: CollectionTaskAttempt) -> None:
        self.attempt = attempt
        self.update_count = 0

    async def get_by_task_fencing(
        self, task_id: UUID, fencing_token: int
    ) -> CollectionTaskAttempt | None:
        if (
            self.attempt.collection_task_id == task_id
            and self.attempt.fencing_token == fencing_token
        ):
            return self.attempt
        return None

    async def update_outcome(self, attempt: CollectionTaskAttempt) -> None:
        self.attempt = attempt
        self.update_count += 1


class EventRepository:
    def __init__(self) -> None:
        self.events: list[StoredCollectionEvent] = []

    async def add(self, event: StoredCollectionEvent) -> None:
        self.events.append(event)


class CallbackConfigRepository:
    def __init__(self) -> None:
        self.config: CollectionJobCallbackConfig | None = None

    async def get_for_job(
        self, job_id: UUID
    ) -> CollectionJobCallbackConfig | None:
        if self.config is not None and self.config.collection_job_id == job_id:
            return self.config
        return None


class WebhookOutboxRepository:
    def __init__(self) -> None:
        self.entries: list[WebhookOutboxEntry] = []

    async def add(self, entry: WebhookOutboxEntry) -> None:
        self.entries.append(entry)


class FakeUnitOfWork:
    def __init__(self) -> None:
        token_hasher = AgentSessionTokenHasher(SecretStr(HASH_KEY))
        lease_tokens = TaskLeaseTokenManager(SecretStr(HASH_KEY))
        job = CollectionJob(
            client_id=CLIENT_ID,
            external_request_id="jobreq_runtime_001",
            idempotency_key_hash="sha256:" + "1" * 64,
            request_payload=CollectionJobPayload(
                source=SourceCode.ZHAOPIN,
                queries=[{"keyword": "Python", "region": "Shanghai"}],
            ),
            status=CollectionState.RUNNING,
            version=2,
        )
        task = CollectionTask(
            collection_job_id=job.id,
            source=SourceCode.ZHAOPIN,
            keyword="Python",
            region="Shanghai",
            page_limit=10,
            status=CollectionState.RUNNING,
            leased_agent_id=AGENT_ID,
            fencing_token=3,
            lease_expires_at=NOW + timedelta(seconds=60),
            attempt_count=1,
        )
        profile = BrowserProfile(
            agent_id=AGENT_ID,
            source=SourceCode.ZHAOPIN,
            profile_key="primary",
            status="busy",
            updated_at=NOW,
        )
        task.leased_profile_id = profile.id
        lease_token = lease_tokens.issue(
            attempt_id=ATTEMPT_ID,
            agent_id=AGENT_ID,
            task_id=task.id,
            fencing_token=task.fencing_token,
        )
        task.lease_token_hash = lease_tokens.hash(lease_token)
        self.lease_token = lease_token
        self.agents = AgentRepository(
            CollectionAgent(
                id=AGENT_ID,
                device_credential_hash="hmac-sha256:" + "2" * 64,
                display_name="Runtime Agent",
                capabilities=AgentCapabilities(
                    supported_sources=[SourceCode.ZHAOPIN]
                ),
                status="online",
                current_task_id=task.id,
                heartbeat_at=NOW,
            )
        )
        self.agent_sessions = SessionRepository(
            AgentSession(
                id=SESSION_ID,
                agent_id=AGENT_ID,
                session_token_hash=token_hasher.hash(SESSION_TOKEN),
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=10),
            )
        )
        self.browser_profiles = ProfileRepository(profile)
        self.collection_jobs = JobRepository(job)
        self.collection_tasks = TaskRepository(task)
        self.task_attempts = AttemptRepository(
            CollectionTaskAttempt(
                id=ATTEMPT_ID,
                collection_task_id=task.id,
                agent_id=AGENT_ID,
                fencing_token=task.fencing_token,
                claimed_at=NOW - timedelta(minutes=1),
            )
        )
        self.collection_events = EventRepository()
        self.callback_configs = CallbackConfigRepository()
        self.webhook_outbox = WebhookOutboxRepository()
        self.commit_count = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


def build_runtime(
    unit_of_work: FakeUnitOfWork, clock: MutableClock
) -> AgentTaskRuntime:
    token_hasher = AgentSessionTokenHasher(SecretStr(HASH_KEY))
    lease_authenticator = ActiveTaskLeaseAuthenticator(
        authenticator=AgentSessionAuthenticator(token_hasher.hash),
        lease_tokens=TaskLeaseTokenManager(SecretStr(HASH_KEY)),
    )
    return AgentTaskRuntime(
        unit_of_work_factory=lambda: cast("object", unit_of_work),  # type: ignore[arg-type]
        lease_authenticator=lease_authenticator,
        lease_seconds=120,
        clock=clock,
    )


def reference(unit_of_work: FakeUnitOfWork) -> LeaseReference:
    task = unit_of_work.collection_tasks.task
    assert task.lease_expires_at is not None
    return LeaseReference(
        agent_id=AGENT_ID,
        task_id=task.id,
        lease_token=unit_of_work.lease_token,
        fencing_token=task.fencing_token,
        lease_expires_at=task.lease_expires_at,
    )


async def test_heartbeat_renews_lease_and_invalidates_old_expiry() -> None:
    unit_of_work = FakeUnitOfWork()
    old_reference = reference(unit_of_work)
    clock = MutableClock(NOW + timedelta(seconds=10))
    runtime = build_runtime(unit_of_work, clock)

    response = await runtime.heartbeat(
        session_token=SecretStr(SESSION_TOKEN),
        task_id=old_reference.task_id,
        request=old_reference,
    )

    assert response.desired_state == "running"
    assert response.lease_expires_at == clock.now + timedelta(seconds=120)
    with pytest.raises(TaskLeaseStateConflict):
        await runtime.heartbeat(
            session_token=SecretStr(SESSION_TOKEN),
            task_id=old_reference.task_id,
            request=old_reference,
        )


async def test_checkpoint_is_monotonic_and_returns_pause_intent() -> None:
    unit_of_work = FakeUnitOfWork()
    task = unit_of_work.collection_tasks.task
    task.page_checkpoint = 3
    task.processed_count = 8
    task.desired_state = DesiredState.PAUSED
    task.status = CollectionState.PAUSING
    unit_of_work.collection_jobs.job.desired_state = DesiredState.PAUSED
    unit_of_work.collection_jobs.job.status = CollectionState.PAUSING
    lease = reference(unit_of_work)
    runtime = build_runtime(unit_of_work, MutableClock(NOW + timedelta(seconds=5)))

    response = await runtime.checkpoint(
        session_token=SecretStr(SESSION_TOKEN),
        task_id=task.id,
        request=TaskCheckpointRequest(
            **lease.model_dump(), page_checkpoint=4, processed_count=9
        ),
    )

    assert response.desired_state == "paused"
    assert task.page_checkpoint == 4
    assert task.processed_count == 9


@pytest.mark.parametrize(
    ("page_checkpoint", "processed_count"),
    [(2, 8), (11, 8), (3, 7)],
)
async def test_checkpoint_rejects_regression_and_out_of_range_values(
    page_checkpoint: int, processed_count: int
) -> None:
    unit_of_work = FakeUnitOfWork()
    task = unit_of_work.collection_tasks.task
    task.page_checkpoint = 3
    task.processed_count = 8
    lease = reference(unit_of_work)

    with pytest.raises(TaskRuntimeStateConflict):
        await build_runtime(unit_of_work, MutableClock(NOW)).checkpoint(
            session_token=SecretStr(SESSION_TOKEN),
            task_id=task.id,
            request=TaskCheckpointRequest(
                **lease.model_dump(),
                page_checkpoint=page_checkpoint,
                processed_count=processed_count,
            ),
        )


async def test_completion_clears_bindings_and_aggregates_success() -> None:
    unit_of_work = FakeUnitOfWork()
    task = unit_of_work.collection_tasks.task
    lease = reference(unit_of_work)
    callback_config = CollectionJobCallbackConfig(
        collection_job_id=unit_of_work.collection_jobs.job.id,
        target_url="http://upstream.internal/callback",
        token_ciphertext=b"encrypted-callback-token",
        token_nonce=b"n" * 12,
        token_fingerprint="hmac-sha256:" + "d" * 64,
        token_expires_at=NOW + timedelta(days=7),
        encryption_key_version="v1",
    )
    unit_of_work.callback_configs.config = callback_config

    await build_runtime(unit_of_work, MutableClock(NOW)).complete(
        session_token=SecretStr(SESSION_TOKEN),
        task_id=task.id,
        request=CompleteTaskRequest(**lease.model_dump(), outcome="completed"),
    )

    assert task.status is CollectionState.SUCCEEDED
    assert task.leased_agent_id is None
    assert task.leased_profile_id is None
    assert task.lease_token_hash is None
    assert task.lease_expires_at is None
    assert unit_of_work.agents.agent.current_task_id is None
    assert unit_of_work.browser_profiles.profile.status == "ready"
    assert unit_of_work.task_attempts.attempt.outcome is TaskAttemptOutcome.COMPLETED
    assert unit_of_work.collection_jobs.job.status is CollectionState.SUCCEEDED
    assert unit_of_work.collection_jobs.job.completed_at == NOW
    assert unit_of_work.collection_jobs.job.version == 3
    assert [event.event_type for event in unit_of_work.collection_events.events] == [
        "collection.job.terminal"
    ]
    assert len(unit_of_work.webhook_outbox.entries) == 1
    outbox = unit_of_work.webhook_outbox.entries[0]
    assert outbox.event_id == unit_of_work.collection_events.events[0].id
    assert outbox.callback_config_id == callback_config.id
    assert outbox.next_attempt_at == NOW


async def test_pause_completion_requires_matching_control_intent() -> None:
    unit_of_work = FakeUnitOfWork()
    task = unit_of_work.collection_tasks.task
    lease = reference(unit_of_work)
    runtime = build_runtime(unit_of_work, MutableClock(NOW))

    with pytest.raises(TaskRuntimeStateConflict):
        await runtime.complete(
            session_token=SecretStr(SESSION_TOKEN),
            task_id=task.id,
            request=CompleteTaskRequest(**lease.model_dump(), outcome="paused"),
        )

    task.desired_state = DesiredState.PAUSED
    task.status = CollectionState.PAUSING
    unit_of_work.collection_jobs.job.desired_state = DesiredState.PAUSED
    unit_of_work.collection_jobs.job.status = CollectionState.PAUSING
    await runtime.complete(
        session_token=SecretStr(SESSION_TOKEN),
        task_id=task.id,
        request=CompleteTaskRequest(**lease.model_dump(), outcome="paused"),
    )
    assert task.status is CollectionState.PAUSED
    assert unit_of_work.collection_jobs.job.status is CollectionState.PAUSED


async def test_release_requeues_running_task_and_finishes_attempt() -> None:
    unit_of_work = FakeUnitOfWork()
    task = unit_of_work.collection_tasks.task
    lease = reference(unit_of_work)

    await build_runtime(unit_of_work, MutableClock(NOW)).release(
        session_token=SecretStr(SESSION_TOKEN),
        task_id=task.id,
        request=ReleaseTaskRequest(
            **lease.model_dump(), failure_code="browser_process_exited"
        ),
    )

    assert task.status is CollectionState.QUEUED
    assert task.failure_code == "browser_process_exited"
    assert unit_of_work.task_attempts.attempt.outcome is TaskAttemptOutcome.FAILED
    assert unit_of_work.collection_jobs.job.status is CollectionState.QUEUED


async def test_expired_lease_recovery_requeues_and_fences_stale_agent() -> None:
    unit_of_work = FakeUnitOfWork()
    task = unit_of_work.collection_tasks.task
    task.lease_expires_at = NOW - timedelta(seconds=1)
    old_fencing_token = task.fencing_token
    recoverer = ExpiredLeaseRecoverer(
        unit_of_work_factory=lambda: cast("object", unit_of_work),  # type: ignore[arg-type]
        clock=MutableClock(NOW),
    )

    recovered = await recoverer.recover_one()

    assert recovered is True
    assert task.status is CollectionState.QUEUED
    assert task.fencing_token == old_fencing_token + 1
    assert task.leased_agent_id is None
    assert unit_of_work.agents.agent.current_task_id is None
    assert unit_of_work.browser_profiles.profile.status == "ready"
    assert unit_of_work.task_attempts.attempt.outcome is TaskAttemptOutcome.LEASE_EXPIRED
    assert unit_of_work.collection_jobs.job.status is CollectionState.QUEUED


async def test_wrong_token_and_path_task_are_uniform_lease_conflicts() -> None:
    unit_of_work = FakeUnitOfWork()
    lease = reference(unit_of_work)
    runtime = build_runtime(unit_of_work, MutableClock(NOW))

    wrong_token = lease.model_copy(update={"lease_token": SecretStr("x" * 32)})
    with pytest.raises(TaskLeaseStateConflict):
        await runtime.heartbeat(
            session_token=SecretStr(SESSION_TOKEN),
            task_id=lease.task_id,
            request=wrong_token,
        )
    with pytest.raises(TaskLeaseStateConflict):
        await runtime.heartbeat(
            session_token=SecretStr(SESSION_TOKEN),
            task_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            request=lease,
        )


class StubRuntime:
    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict

    async def heartbeat(self, **_: object) -> object:
        if self.conflict:
            raise TaskLeaseStateConflict
        return {
            "desiredState": "running",
            "leaseExpiresAt": (NOW + timedelta(seconds=120)).isoformat(),
        }

    async def complete(self, **_: object) -> None:
        if self.conflict:
            raise TaskRuntimeStateConflict


def runtime_http_app(runtime: StubRuntime) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.agent_task_runtime = runtime
    return app


def lease_payload(unit_of_work: FakeUnitOfWork) -> dict[str, object]:
    return reference(unit_of_work).model_dump(mode="json", by_alias=True)


async def test_runtime_http_routes_map_success_and_missing_session() -> None:
    unit_of_work = FakeUnitOfWork()
    task_id = unit_of_work.collection_tasks.task.id
    async with AsyncClient(
        transport=ASGITransport(app=runtime_http_app(StubRuntime())),
        base_url="http://test",
    ) as client:
        missing = await client.post(
            f"/v1/agent/tasks/{task_id}/heartbeat",
            json=lease_payload(unit_of_work),
        )
        heartbeat = await client.post(
            f"/v1/agent/tasks/{task_id}/heartbeat",
            headers={"Authorization": "Bearer test-session-token"},
            json=lease_payload(unit_of_work),
        )
        complete_payload = lease_payload(unit_of_work)
        complete_payload["outcome"] = "completed"
        completed = await client.post(
            f"/v1/agent/tasks/{task_id}/complete",
            headers={"Authorization": "Bearer test-session-token"},
            json=complete_payload,
        )

    assert missing.status_code == 401
    assert heartbeat.status_code == 200
    assert heartbeat.json()["desiredState"] == "running"
    assert completed.status_code == 204
    assert completed.content == b""


async def test_runtime_http_routes_map_lease_conflicts_to_409() -> None:
    unit_of_work = FakeUnitOfWork()
    task_id = unit_of_work.collection_tasks.task.id
    async with AsyncClient(
        transport=ASGITransport(app=runtime_http_app(StubRuntime(conflict=True))),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/v1/agent/tasks/{task_id}/heartbeat",
            headers={"Authorization": "Bearer test-session-token"},
            json=lease_payload(unit_of_work),
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "task lease is not active"}

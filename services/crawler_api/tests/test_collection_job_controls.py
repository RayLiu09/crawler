from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from crawler_api.api.v1.collection_jobs import router
from crawler_api.application.caller_authentication import (
    CallerAccessTokenAuthenticationDenied,
    CallerAccessTokenAuthenticator,
    CallerAccessTokenInsufficientScope,
)
from crawler_api.application.collection_jobs import (
    CollectionJobControlAction,
    CollectionJobControlConflict,
    CollectionJobController,
    CollectionJobDetails,
    CollectionJobNotFound,
    CollectionJobReader,
    summarize_job,
)
from crawler_api.domain.callbacks import CollectionJobCallbackConfig
from crawler_api.domain.entities import (
    ApiAccessToken,
    ApiClient,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.enums import CollectionState, DesiredState
from crawler_api.domain.events import StoredCollectionEvent
from crawler_api.domain.webhooks import WebhookOutboxEntry
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import ApiScope, SourceCode
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr

CLIENT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_CLIENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TOKEN_ID = UUID("22222222-2222-4222-8222-222222222222")
ACCESS_TOKEN = "cat_collection_job_control_token_123456"
TOKEN_HASH_KEY = "api-token-hash-key-that-is-at-least-32-bytes"
CREATED_AT = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
CONTROLLED_AT = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


class FakeApiClientRepository:
    def __init__(self, client: ApiClient) -> None:
        self.client = client

    async def get_for_share(self, client_id: UUID) -> ApiClient | None:
        return self.client if self.client.id == client_id else None


class FakeApiAccessTokenRepository:
    def __init__(self, token: ApiAccessToken) -> None:
        self.token = token
        self.last_used_updates: list[tuple[UUID, datetime]] = []

    async def get_by_token_hash(self, token_hash: str) -> ApiAccessToken | None:
        return self.token if self.token.token_hash == token_hash else None

    async def get(self, token_id: UUID) -> ApiAccessToken | None:
        return self.token if self.token.id == token_id else None

    async def update_last_used(self, token_id: UUID, last_used_at: datetime) -> None:
        self.last_used_updates.append((token_id, last_used_at))


class FakeCollectionJobRepository:
    def __init__(self, job: CollectionJob) -> None:
        self.job = job
        self.update_count = 0

    async def get_for_client(
        self, job_id: UUID, client_id: UUID
    ) -> CollectionJob | None:
        if self.job.id == job_id and self.job.client_id == client_id:
            return self.job
        return None

    async def get_for_client_for_update(
        self, job_id: UUID, client_id: UUID
    ) -> CollectionJob | None:
        return await self.get_for_client(job_id, client_id)

    async def update_control_state(
        self, job: CollectionJob, updated_at: datetime
    ) -> None:
        assert job is self.job
        assert updated_at == CONTROLLED_AT
        self.update_count += 1


class FakeCollectionTaskRepository:
    def __init__(self, tasks: list[CollectionTask]) -> None:
        self.tasks = tasks
        self.updated_task_ids: list[UUID] = []

    async def list_for_job(self, job_id: UUID) -> list[CollectionTask]:
        return [task for task in self.tasks if task.collection_job_id == job_id]

    async def list_for_job_for_update(self, job_id: UUID) -> list[CollectionTask]:
        return await self.list_for_job(job_id)

    async def update_control_state(
        self, task: CollectionTask, updated_at: datetime
    ) -> None:
        assert updated_at == CONTROLLED_AT
        self.updated_task_ids.append(task.id)


class FakeCollectionEventRepository:
    def __init__(self) -> None:
        self.events: list[StoredCollectionEvent] = []

    async def add(self, event: StoredCollectionEvent) -> None:
        self.events.append(event)


class FakeCallbackConfigRepository:
    def __init__(self, config: CollectionJobCallbackConfig | None) -> None:
        self.config = config

    async def get_for_job(
        self, job_id: UUID
    ) -> CollectionJobCallbackConfig | None:
        if self.config is not None and self.config.collection_job_id == job_id:
            return self.config
        return None


class FakeWebhookOutboxRepository:
    def __init__(self) -> None:
        self.entries: list[WebhookOutboxEntry] = []

    async def add(self, entry: WebhookOutboxEntry) -> None:
        self.entries.append(entry)


class FakeUnitOfWork:
    def __init__(
        self,
        *,
        job: CollectionJob,
        tasks: list[CollectionTask],
        token_scopes: list[ApiScope] | None = None,
        callback_config: CollectionJobCallbackConfig | None = None,
    ) -> None:
        client = ApiClient(
            id=CLIENT_ID,
            tenant_id="control-tenant",
            display_name="Control Tenant",
            allowed_scopes=list(ApiScope),
            config_fingerprint="sha256:" + "a" * 64,
        )
        token = ApiAccessToken(
            id=TOKEN_ID,
            client_id=CLIENT_ID,
            token_hash=token_hasher().hash(ACCESS_TOKEN),
            issued_via_key_fingerprint="sha256:" + "b" * 64,
            scopes=token_scopes or list(ApiScope),
            issued_at=CREATED_AT,
            expires_at=CONTROLLED_AT + timedelta(hours=1),
        )
        self.api_clients = FakeApiClientRepository(client)
        self.api_access_tokens = FakeApiAccessTokenRepository(token)
        self.collection_jobs = FakeCollectionJobRepository(job)
        self.collection_tasks = FakeCollectionTaskRepository(tasks)
        self.collection_events = FakeCollectionEventRepository()
        self.callback_configs = FakeCallbackConfigRepository(callback_config)
        self.webhook_outbox = FakeWebhookOutboxRepository()
        self.commit_count = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


def token_hasher() -> AccessTokenHasher:
    return AccessTokenHasher(SecretStr(TOKEN_HASH_KEY))


def build_job(
    *,
    client_id: UUID = CLIENT_ID,
    desired_state: DesiredState = DesiredState.RUNNING,
    status: CollectionState = CollectionState.QUEUED,
) -> CollectionJob:
    return CollectionJob(
        client_id=client_id,
        external_request_id="jobreq_control_001",
        idempotency_key_hash="sha256:" + "c" * 64,
        request_payload=CollectionJobPayload(
            source=SourceCode.ZHAOPIN,
            queries=[{"keyword": "Python", "region": "Beijing"}],
        ),
        desired_state=desired_state,
        status=status,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def build_task(
    job: CollectionJob,
    status: CollectionState,
    *,
    desired_state: DesiredState = DesiredState.RUNNING,
    processed_count: int = 0,
    failure_code: str | None = None,
) -> CollectionTask:
    return CollectionTask(
        collection_job_id=job.id,
        source=SourceCode.ZHAOPIN,
        keyword="Python",
        region="Beijing",
        page_limit=10,
        desired_state=desired_state,
        status=status,
        processed_count=processed_count,
        failure_code=failure_code,
    )


def build_callback_config(job: CollectionJob) -> CollectionJobCallbackConfig:
    return CollectionJobCallbackConfig(
        collection_job_id=job.id,
        target_url="http://upstream.internal/callback",
        token_ciphertext=b"encrypted-callback-token",
        token_nonce=b"n" * 12,
        token_fingerprint="hmac-sha256:" + "d" * 64,
        token_expires_at=CONTROLLED_AT + timedelta(days=7),
        encryption_key_version="v1",
    )


def build_reader(unit_of_work: FakeUnitOfWork) -> CollectionJobReader:
    return CollectionJobReader(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=CallerAccessTokenAuthenticator(token_hasher().hash),
        clock=lambda: CONTROLLED_AT,
    )


def build_controller(unit_of_work: FakeUnitOfWork) -> CollectionJobController:
    return CollectionJobController(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=CallerAccessTokenAuthenticator(token_hasher().hash),
        clock=lambda: CONTROLLED_AT,
    )


async def control(
    controller: CollectionJobController,
    job_id: UUID,
    action: CollectionJobControlAction,
) -> CollectionJobDetails:
    return await controller.control(
        access_token=SecretStr(ACCESS_TOKEN),
        job_id=job_id,
        action=action,
    )


async def test_reader_returns_tenant_scoped_counts_failures_and_use_time() -> None:
    job = build_job(status=CollectionState.RUNNING)
    tasks = [
        build_task(job, CollectionState.RUNNING, processed_count=5),
        build_task(
            job,
            CollectionState.FAILED,
            processed_count=2,
            failure_code="navigation_timeout",
        ),
    ]
    unit_of_work = FakeUnitOfWork(job=job, tasks=tasks)

    details = await build_reader(unit_of_work).get(
        access_token=SecretStr(ACCESS_TOKEN), job_id=job.id
    )

    assert details.task_status_counts == {
        CollectionState.RUNNING: 1,
        CollectionState.FAILED: 1,
    }
    assert details.processed_count == 7
    assert details.failure_code_counts == {"navigation_timeout": 1}
    assert unit_of_work.api_access_tokens.last_used_updates == [
        (TOKEN_ID, CONTROLLED_AT)
    ]
    assert unit_of_work.commit_count == 1


async def test_reader_hides_jobs_owned_by_another_client() -> None:
    job = build_job(client_id=OTHER_CLIENT_ID)
    unit_of_work = FakeUnitOfWork(job=job, tasks=[])

    with pytest.raises(CollectionJobNotFound):
        await build_reader(unit_of_work).get(
            access_token=SecretStr(ACCESS_TOKEN), job_id=job.id
        )

    assert unit_of_work.commit_count == 0


async def test_pause_queued_job_is_immediate_and_idempotent() -> None:
    job = build_job()
    tasks = [build_task(job, CollectionState.QUEUED) for _ in range(2)]
    unit_of_work = FakeUnitOfWork(job=job, tasks=tasks)
    controller = build_controller(unit_of_work)

    first = await control(controller, job.id, CollectionJobControlAction.PAUSE)
    second = await control(controller, job.id, CollectionJobControlAction.PAUSE)

    assert first.job.status is CollectionState.PAUSED
    assert first.job.desired_state is DesiredState.PAUSED
    assert all(task.status is CollectionState.PAUSED for task in tasks)
    assert all(task.desired_state is DesiredState.PAUSED for task in tasks)
    assert second.job.version == 2
    assert unit_of_work.collection_jobs.update_count == 1
    assert len(unit_of_work.collection_tasks.updated_task_ids) == 2
    assert [event.event_type for event in unit_of_work.collection_events.events] == [
        "collection.job.desired_state_changed"
    ]
    assert unit_of_work.commit_count == 2


async def test_pause_running_job_waits_for_agent_and_preserves_terminal_tasks() -> None:
    job = build_job(status=CollectionState.RUNNING)
    running = build_task(job, CollectionState.RUNNING)
    queued = build_task(job, CollectionState.QUEUED)
    succeeded = build_task(job, CollectionState.SUCCEEDED, processed_count=4)
    unit_of_work = FakeUnitOfWork(job=job, tasks=[running, queued, succeeded])

    details = await control(
        build_controller(unit_of_work), job.id, CollectionJobControlAction.PAUSE
    )

    assert details.job.status is CollectionState.PAUSING
    assert running.status is CollectionState.PAUSING
    assert queued.status is CollectionState.PAUSED
    assert succeeded.status is CollectionState.SUCCEEDED
    assert succeeded.desired_state is DesiredState.RUNNING
    assert set(unit_of_work.collection_tasks.updated_task_ids) == {
        running.id,
        queued.id,
    }


async def test_resume_paused_job_requeues_from_existing_checkpoint() -> None:
    job = build_job(
        desired_state=DesiredState.PAUSED,
        status=CollectionState.PAUSED,
    )
    task = build_task(
        job,
        CollectionState.PAUSED,
        desired_state=DesiredState.PAUSED,
    )
    task.page_checkpoint = 6
    unit_of_work = FakeUnitOfWork(job=job, tasks=[task])
    controller = build_controller(unit_of_work)

    first = await control(controller, job.id, CollectionJobControlAction.RESUME)
    second = await control(controller, job.id, CollectionJobControlAction.RESUME)

    assert first.job.status is CollectionState.QUEUED
    assert first.job.desired_state is DesiredState.RUNNING
    assert task.status is CollectionState.QUEUED
    assert task.desired_state is DesiredState.RUNNING
    assert task.page_checkpoint == 6
    assert second.job.version == 2
    assert unit_of_work.collection_jobs.update_count == 1


async def test_resume_is_rejected_until_running_tasks_finish_pausing() -> None:
    job = build_job(
        desired_state=DesiredState.PAUSED,
        status=CollectionState.PAUSING,
    )
    task = build_task(
        job,
        CollectionState.PAUSING,
        desired_state=DesiredState.PAUSED,
    )
    unit_of_work = FakeUnitOfWork(job=job, tasks=[task])

    with pytest.raises(CollectionJobControlConflict):
        await control(
            build_controller(unit_of_work),
            job.id,
            CollectionJobControlAction.RESUME,
        )

    assert unit_of_work.commit_count == 0
    assert job.version == 1


async def test_cancel_queued_job_is_immediate_and_sets_completion_time() -> None:
    job = build_job()
    task = build_task(job, CollectionState.QUEUED)
    callback_config = build_callback_config(job)
    unit_of_work = FakeUnitOfWork(
        job=job,
        tasks=[task],
        callback_config=callback_config,
    )

    details = await control(
        build_controller(unit_of_work), job.id, CollectionJobControlAction.CANCEL
    )

    assert details.job.status is CollectionState.CANCELLED
    assert details.job.desired_state is DesiredState.CANCELLED
    assert details.job.completed_at == CONTROLLED_AT
    assert [event.event_type for event in unit_of_work.collection_events.events] == [
        "collection.job.desired_state_changed",
        "collection.job.terminal",
    ]
    assert [entry.event_id for entry in unit_of_work.webhook_outbox.entries] == [
        event.id for event in unit_of_work.collection_events.events
    ]
    assert all(
        entry.callback_config_id == callback_config.id
        and entry.next_attempt_at == CONTROLLED_AT
        for entry in unit_of_work.webhook_outbox.entries
    )
    assert task.status is CollectionState.CANCELLED
    assert task.desired_state is DesiredState.CANCELLED


async def test_cancel_running_job_enters_cancelling_and_is_idempotent() -> None:
    job = build_job(status=CollectionState.RUNNING)
    running = build_task(job, CollectionState.RUNNING)
    queued = build_task(job, CollectionState.QUEUED)
    unit_of_work = FakeUnitOfWork(job=job, tasks=[running, queued])
    controller = build_controller(unit_of_work)

    first = await control(controller, job.id, CollectionJobControlAction.CANCEL)
    second = await control(controller, job.id, CollectionJobControlAction.CANCEL)

    assert first.job.status is CollectionState.CANCELLING
    assert first.job.completed_at is None
    assert running.status is CollectionState.CANCELLING
    assert queued.status is CollectionState.CANCELLED
    assert second.job.version == 2
    assert unit_of_work.collection_jobs.update_count == 1


@pytest.mark.parametrize(
    "action",
    list(CollectionJobControlAction),
)
async def test_completed_job_rejects_new_control(action: CollectionJobControlAction) -> None:
    job = build_job(status=CollectionState.SUCCEEDED)
    unit_of_work = FakeUnitOfWork(
        job=job,
        tasks=[build_task(job, CollectionState.SUCCEEDED)],
    )

    with pytest.raises(CollectionJobControlConflict):
        await control(build_controller(unit_of_work), job.id, action)


async def test_control_requires_control_scope() -> None:
    job = build_job()
    unit_of_work = FakeUnitOfWork(
        job=job,
        tasks=[build_task(job, CollectionState.QUEUED)],
        token_scopes=[ApiScope.COLLECTION_READ],
    )

    with pytest.raises(CallerAccessTokenInsufficientScope):
        await control(
            build_controller(unit_of_work), job.id, CollectionJobControlAction.PAUSE
        )


class StubReader:
    def __init__(self, result: CollectionJobDetails | Exception) -> None:
        self.result = result

    async def get(self, **kwargs: object) -> CollectionJobDetails:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubController:
    def __init__(self, result: CollectionJobDetails | Exception) -> None:
        self.result = result

    async def control(self, **kwargs: object) -> CollectionJobDetails:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def build_route_app(
    *,
    reader_result: CollectionJobDetails | Exception,
    controller_result: CollectionJobDetails | Exception,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.collection_job_reader = StubReader(reader_result)
    app.state.collection_job_controller = StubController(controller_result)
    return app


def route_details() -> CollectionJobDetails:
    job = build_job()
    return summarize_job(job, [build_task(job, CollectionState.QUEUED)])


async def request_job(
    app: FastAPI, method: str, path: str, *, with_token: bool = True
) -> Response:
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"} if with_token else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers)


async def test_query_route_returns_job_details_contract() -> None:
    details = route_details()
    app = build_route_app(reader_result=details, controller_result=details)

    response = await request_job(app, "GET", f"/v1/collection-jobs/{details.job.id}")

    assert response.status_code == 200
    assert response.json() == {
        "collectionJobId": str(details.job.id),
        "externalRequestId": "jobreq_control_001",
        "source": "zhaopin",
        "desiredState": "running",
        "status": "queued",
        "version": 1,
        "taskStatusCounts": {
            "total": 1,
            "queued": 1,
            "running": 0,
            "pausing": 0,
            "paused": 0,
            "awaitingHuman": 0,
            "cancelling": 0,
            "cancelled": 0,
            "succeeded": 0,
            "partiallySucceeded": 0,
            "failed": 0,
        },
        "processedCount": 0,
        "failureCodeCounts": {},
        "createdAt": "2026-09-17T09:00:00Z",
        "updatedAt": "2026-09-17T09:00:00Z",
        "completedAt": None,
    }


@pytest.mark.parametrize(
    ("reader_result", "with_token", "expected_status"),
    [
        (route_details(), False, 401),
        (CallerAccessTokenAuthenticationDenied(), True, 401),
        (CallerAccessTokenInsufficientScope(), True, 403),
        (CollectionJobNotFound(), True, 404),
    ],
)
async def test_query_route_maps_access_errors(
    reader_result: CollectionJobDetails | Exception,
    with_token: bool,
    expected_status: int,
) -> None:
    details = route_details()
    app = build_route_app(
        reader_result=reader_result,
        controller_result=details,
    )

    response = await request_job(
        app,
        "GET",
        f"/v1/collection-jobs/{details.job.id}",
        with_token=with_token,
    )

    assert response.status_code == expected_status


async def test_control_route_returns_details_and_maps_conflict() -> None:
    details = route_details()
    success_app = build_route_app(reader_result=details, controller_result=details)
    conflict_app = build_route_app(
        reader_result=details,
        controller_result=CollectionJobControlConflict(),
    )

    success = await request_job(
        success_app,
        "POST",
        f"/v1/collection-jobs/{details.job.id}/pause",
    )
    conflict = await request_job(
        conflict_app,
        "POST",
        f"/v1/collection-jobs/{details.job.id}/resume",
    )

    assert success.status_code == 200
    assert conflict.status_code == 409


async def test_control_route_rejects_unknown_action() -> None:
    details = route_details()
    app = build_route_app(reader_result=details, controller_result=details)

    response = await request_job(
        app,
        "POST",
        f"/v1/collection-jobs/{uuid4()}/restart",
    )

    assert response.status_code == 422

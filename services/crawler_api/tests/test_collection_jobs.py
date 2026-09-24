from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.api.v1.collection_jobs import router
from crawler_api.application.caller_authentication import (
    CallerAccessTokenAuthenticationDenied,
    CallerAccessTokenAuthenticator,
    CallerAccessTokenInsufficientScope,
)
from crawler_api.application.collection_jobs import (
    CollectionJobCallbackInvalid,
    CollectionJobIdempotencyConflict,
    CollectionJobSubmitter,
    SubmittedCollectionJob,
    hash_idempotency_key,
)
from crawler_api.application.tenant_allowlist import TenantAllowlist
from crawler_api.domain.callbacks import CollectionJobCallbackConfig
from crawler_api.domain.entities import (
    ApiAccessToken,
    ApiClient,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.events import StoredCollectionEvent
from crawler_api.domain.webhooks import WebhookOutboxEntry
from crawler_api.infrastructure.security.callback_tokens import (
    CallbackTokenProtector,
    ProtectedCallbackToken,
)
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from crawler_contracts.common import ApiScope
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

CLIENT_ID = UUID("11111111-1111-4111-8111-111111111111")
TOKEN_ID = UUID("22222222-2222-4222-8222-222222222222")
ACCESS_TOKEN = "cat_collection_job_access_token_123456789012"
TOKEN_HASH_KEY = "api-token-hash-key-that-is-at-least-32-bytes"
IDEMPOTENCY_KEY = "create-job-operation-001"
SUBMITTED_AT = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
CALLBACK_URL = "http://upstream.internal/api/job-collection/events"
CALLBACK_TOKEN = "callback_token_12345678901234567890123456789"
CALLBACK_ENCRYPTION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
CALLBACK_FINGERPRINT_KEY = "callback-fingerprint-key-that-is-at-least-32-bytes"


class FakeApiClientRepository:
    def __init__(self, client: ApiClient | None) -> None:
        self.client = client
        self.shared_locks: list[UUID] = []

    async def get_for_share(self, client_id: UUID) -> ApiClient | None:
        self.shared_locks.append(client_id)
        return self.client if self.client is not None and self.client.id == client_id else None


class FakeApiAccessTokenRepository:
    def __init__(
        self,
        initial_token: ApiAccessToken | None,
        current_token: ApiAccessToken | None = None,
    ) -> None:
        self.initial_token = initial_token
        self.current_token = current_token or initial_token
        self.last_used_updates: list[tuple[UUID, datetime]] = []

    async def get_by_token_hash(self, token_hash: str) -> ApiAccessToken | None:
        if self.initial_token is None or self.initial_token.token_hash != token_hash:
            return None
        return self.initial_token

    async def get(self, token_id: UUID) -> ApiAccessToken | None:
        if self.current_token is None or self.current_token.id != token_id:
            return None
        return self.current_token

    async def update_last_used(self, token_id: UUID, last_used_at: datetime) -> None:
        self.last_used_updates.append((token_id, last_used_at))


class FakeCollectionJobRepository:
    def __init__(self) -> None:
        self.jobs: list[CollectionJob] = []
        self.force_conflict_winner: CollectionJob | None = None

    async def get_by_idempotency_hash(
        self, client_id: UUID, idempotency_key_hash: str
    ) -> CollectionJob | None:
        return next(
            (
                job
                for job in self.jobs
                if job.client_id == client_id
                and job.idempotency_key_hash == idempotency_key_hash
            ),
            None,
        )

    async def get_by_external_request_id(
        self, client_id: UUID, external_request_id: str
    ) -> CollectionJob | None:
        return next(
            (
                job
                for job in self.jobs
                if job.client_id == client_id
                and job.external_request_id == external_request_id
            ),
            None,
        )

    async def try_add(self, job: CollectionJob) -> bool:
        if self.force_conflict_winner is not None:
            self.jobs.append(self.force_conflict_winner)
            self.force_conflict_winner = None
            return False
        self.jobs.append(job)
        return True


class FakeCollectionTaskRepository:
    def __init__(self) -> None:
        self.tasks: list[CollectionTask] = []

    async def add(self, task: CollectionTask) -> None:
        self.tasks.append(task)


class FakeCollectionEventRepository:
    def __init__(self) -> None:
        self.events: list[StoredCollectionEvent] = []

    async def add(self, event: StoredCollectionEvent) -> None:
        self.events.append(event)


class FakeCallbackConfigRepository:
    def __init__(self) -> None:
        self.configs: dict[UUID, CollectionJobCallbackConfig] = {}

    async def add(self, config: CollectionJobCallbackConfig) -> None:
        self.configs[config.collection_job_id] = config

    async def get_for_job(
        self, job_id: UUID
    ) -> CollectionJobCallbackConfig | None:
        return self.configs.get(job_id)


class FakeWebhookOutboxRepository:
    def __init__(self) -> None:
        self.entries: list[WebhookOutboxEntry] = []

    async def add(self, entry: WebhookOutboxEntry) -> None:
        self.entries.append(entry)


class FakeUnitOfWork:
    def __init__(
        self,
        *,
        client: ApiClient | None,
        initial_token: ApiAccessToken | None,
        current_token: ApiAccessToken | None = None,
    ) -> None:
        self.api_clients = FakeApiClientRepository(client)
        self.api_access_tokens = FakeApiAccessTokenRepository(
            initial_token, current_token
        )
        self.collection_jobs = FakeCollectionJobRepository()
        self.collection_tasks = FakeCollectionTaskRepository()
        self.collection_events = FakeCollectionEventRepository()
        self.callback_configs = FakeCallbackConfigRepository()
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


def build_client(*, status: str = "active") -> ApiClient:
    return ApiClient(
        id=CLIENT_ID,
        tenant_id="collection-job-tenant",
        display_name="Collection Job Tenant",
        allowed_scopes=list(ApiScope),
        config_fingerprint="sha256:" + "a" * 64,
        status=status,
    )


def build_token(
    *,
    scopes: list[ApiScope] | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> ApiAccessToken:
    return ApiAccessToken(
        id=TOKEN_ID,
        client_id=CLIENT_ID,
        token_hash=token_hasher().hash(ACCESS_TOKEN),
        issued_via_key_fingerprint="sha256:" + "b" * 64,
        scopes=scopes or [ApiScope.COLLECTION_WRITE],
        issued_at=SUBMITTED_AT - timedelta(minutes=1),
        expires_at=expires_at or SUBMITTED_AT + timedelta(hours=1),
        revoked_at=revoked_at,
    )


def build_request(
    *,
    external_request_id: str = "jobreq_001",
    keyword: str = "Python",
    callback: dict[str, object] | None = None,
) -> SubmitCollectionJobRequest:
    payload: dict[str, object] = {
        "externalRequestId": external_request_id,
        "source": "zhaopin",
        "queries": [
            {"keyword": keyword, "region": "Beijing", "pageLimit": 2},
            {"keyword": "Java", "region": "Shanghai", "pageLimit": 3},
        ],
        "metadata": {"requestClass": "integration"},
    }
    if callback is not None:
        payload["callback"] = callback
    return SubmitCollectionJobRequest.model_validate(payload)


def callback_payload(
    *,
    token: str = CALLBACK_TOKEN,
    url: str = CALLBACK_URL,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "url": url,
        "accessToken": token,
        "tokenType": "Bearer",
        "expiresAt": expires_at.isoformat() if expires_at is not None else None,
    }


def callback_protector() -> CallbackTokenProtector:
    return CallbackTokenProtector(
        encryption_key=SecretStr(CALLBACK_ENCRYPTION_KEY),
        fingerprint_key=SecretStr(CALLBACK_FINGERPRINT_KEY),
        encryption_key_version="v1",
    )


def tenant_allowlist() -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": "collection-job-tenant",
                    "displayName": "Collection Job Tenant",
                    "tenantKeyHashes": ["sha256:" + "1" * 64],
                    "callbackAllowlist": [CALLBACK_URL],
                }
            ],
        }
    )


def build_submitter(unit_of_work: FakeUnitOfWork) -> CollectionJobSubmitter:
    return CollectionJobSubmitter(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=CallerAccessTokenAuthenticator(token_hasher().hash),
        tenant_allowlist=tenant_allowlist(),
        callback_token_protector=callback_protector(),
        clock=lambda: SUBMITTED_AT,
    )


async def submit(
    submitter: CollectionJobSubmitter,
    *,
    request: SubmitCollectionJobRequest | None = None,
    idempotency_key: str = IDEMPOTENCY_KEY,
) -> SubmittedCollectionJob:
    return await submitter.submit(
        access_token=SecretStr(ACCESS_TOKEN),
        idempotency_key=SecretStr(idempotency_key),
        request=request or build_request(),
    )


async def test_submit_creates_job_scope_snapshot_and_one_task_per_query() -> None:
    token = build_token()
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=token)

    result = await submit(build_submitter(unit_of_work))

    assert result.created
    assert result.task_count == 2
    assert result.job.idempotency_key_hash == hash_idempotency_key(
        SecretStr(IDEMPOTENCY_KEY)
    )
    assert result.job.request_payload == build_request().canonical_payload()
    assert "external_request_id" not in result.job.request_payload.model_fields_set
    assert len(unit_of_work.collection_tasks.tasks) == 2
    assert len(unit_of_work.collection_events.events) == 1
    assert unit_of_work.webhook_outbox.entries == []
    accepted = unit_of_work.collection_events.events[0]
    assert accepted.event_type == "collection.job.accepted"
    assert accepted.job_version == 1
    assert accepted.payload.model_dump(mode="json", by_alias=True) == {
        "source": "zhaopin",
        "desiredState": "running",
        "status": "queued",
        "taskCount": 2,
    }
    assert [task.keyword for task in unit_of_work.collection_tasks.tasks] == [
        "Python",
        "Java",
    ]
    assert all(
        task.collection_job_id == result.job.id
        for task in unit_of_work.collection_tasks.tasks
    )
    assert unit_of_work.api_access_tokens.last_used_updates == [
        (TOKEN_ID, SUBMITTED_AT)
    ]
    assert token.last_used_at == SUBMITTED_AT
    assert unit_of_work.commit_count == 1


async def test_same_idempotency_key_and_payload_replays_without_new_tasks() -> None:
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=build_token())
    submitter = build_submitter(unit_of_work)

    first = await submit(submitter)
    replay = await submit(submitter)

    assert first.created
    assert not replay.created
    assert replay.job.id == first.job.id
    assert len(unit_of_work.collection_jobs.jobs) == 1
    assert len(unit_of_work.collection_tasks.tasks) == 2
    assert len(unit_of_work.collection_events.events) == 1
    assert unit_of_work.commit_count == 2


async def test_same_external_request_and_payload_replays_with_new_operation_key() -> None:
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=build_token())
    submitter = build_submitter(unit_of_work)

    first = await submit(submitter)
    replay = await submit(submitter, idempotency_key="another-operation-key")

    assert not replay.created
    assert replay.job.id == first.job.id
    assert len(unit_of_work.collection_jobs.jobs) == 1
    assert len(unit_of_work.collection_tasks.tasks) == 2


async def test_callback_is_encrypted_excluded_from_snapshot_and_replayed() -> None:
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=build_token())
    submitter = build_submitter(unit_of_work)
    request = build_request(callback=callback_payload())

    first = await submit(submitter, request=request)
    replay = await submit(submitter, request=request)

    assert first.callback_token_expires_at == SUBMITTED_AT + timedelta(days=7)
    assert replay.callback_token_expires_at == first.callback_token_expires_at
    assert len(unit_of_work.callback_configs.configs) == 1
    assert len(unit_of_work.webhook_outbox.entries) == 1
    config = unit_of_work.callback_configs.configs[first.job.id]
    outbox = unit_of_work.webhook_outbox.entries[0]
    assert outbox.event_id == unit_of_work.collection_events.events[0].id
    assert outbox.callback_config_id == config.id
    assert outbox.next_attempt_at == SUBMITTED_AT
    assert outbox.attempt_count == 0
    assert config.target_url == CALLBACK_URL
    assert config.auth_scheme == "bearer"
    assert config.credential_version == 1
    assert config.encryption_key_version == "v1"
    assert len(config.token_nonce) == 12
    assert CALLBACK_TOKEN.encode() not in config.token_ciphertext
    assert "callback" not in first.job.request_payload.model_dump(
        mode="json", by_alias=True
    )
    decrypted = callback_protector().decrypt(
        ProtectedCallbackToken(
            ciphertext=config.token_ciphertext,
            nonce=config.token_nonce,
            fingerprint=config.token_fingerprint,
            encryption_key_version=config.encryption_key_version,
        ),
        callback_config_id=config.id,
        collection_job_id=config.collection_job_id,
        credential_version=config.credential_version,
    )
    assert decrypted.get_secret_value() == CALLBACK_TOKEN


@pytest.mark.parametrize(
    "callback",
    [
        callback_payload(token=ACCESS_TOKEN),
        callback_payload(url="https://not-allowed.example/callback"),
        callback_payload(expires_at=SUBMITTED_AT),
        callback_payload(expires_at=SUBMITTED_AT + timedelta(days=7, seconds=1)),
    ],
)
async def test_invalid_callback_is_rejected_before_job_creation(
    callback: dict[str, object],
) -> None:
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=build_token())

    with pytest.raises(CollectionJobCallbackInvalid):
        await submit(build_submitter(unit_of_work), request=build_request(callback=callback))

    assert unit_of_work.collection_jobs.jobs == []
    assert unit_of_work.callback_configs.configs == {}


async def test_callback_change_conflicts_with_idempotent_replay() -> None:
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=build_token())
    submitter = build_submitter(unit_of_work)
    await submit(submitter, request=build_request(callback=callback_payload()))

    with pytest.raises(CollectionJobIdempotencyConflict):
        await submit(
            submitter,
            request=build_request(
                callback=callback_payload(
                    token="different_callback_token_123456789012345678901"
                )
            ),
        )


@pytest.mark.parametrize(
    ("second_request", "second_key"),
    [
        (build_request(keyword="Go"), IDEMPOTENCY_KEY),
        (build_request(external_request_id="jobreq_002"), IDEMPOTENCY_KEY),
        (build_request(keyword="Go"), "another-operation-key"),
    ],
)
async def test_reused_identifier_with_different_request_returns_conflict(
    second_request: SubmitCollectionJobRequest,
    second_key: str,
) -> None:
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=build_token())
    submitter = build_submitter(unit_of_work)
    await submit(submitter)

    with pytest.raises(CollectionJobIdempotencyConflict):
        await submit(submitter, request=second_request, idempotency_key=second_key)

    assert len(unit_of_work.collection_jobs.jobs) == 1
    assert len(unit_of_work.collection_tasks.tasks) == 2


async def test_on_conflict_winner_is_reloaded_without_creating_tasks() -> None:
    unit_of_work = FakeUnitOfWork(client=build_client(), initial_token=build_token())
    request = build_request()
    winner = CollectionJob(
        client_id=CLIENT_ID,
        external_request_id=request.external_request_id,
        idempotency_key_hash=hash_idempotency_key(SecretStr(IDEMPOTENCY_KEY)),
        request_payload=request.canonical_payload(),
    )
    unit_of_work.collection_jobs.force_conflict_winner = winner

    result = await submit(build_submitter(unit_of_work), request=request)

    assert not result.created
    assert result.job.id == winner.id
    assert unit_of_work.collection_tasks.tasks == []


@pytest.mark.parametrize(
    ("client", "token", "expected_error"),
    [
        (build_client(), None, CallerAccessTokenAuthenticationDenied),
        (build_client(status="disabled"), build_token(), CallerAccessTokenAuthenticationDenied),
        (
            build_client(),
            build_token(revoked_at=SUBMITTED_AT - timedelta(seconds=1)),
            CallerAccessTokenAuthenticationDenied,
        ),
        (
            build_client(),
            build_token(expires_at=SUBMITTED_AT),
            CallerAccessTokenAuthenticationDenied,
        ),
        (
            build_client(),
            build_token(scopes=[ApiScope.COLLECTION_READ]),
            CallerAccessTokenInsufficientScope,
        ),
    ],
)
async def test_invalid_token_states_and_scope_cannot_create_jobs(
    client: ApiClient | None,
    token: ApiAccessToken | None,
    expected_error: type[Exception],
) -> None:
    unit_of_work = FakeUnitOfWork(client=client, initial_token=token)

    with pytest.raises(expected_error):
        await submit(build_submitter(unit_of_work))

    assert unit_of_work.collection_jobs.jobs == []
    assert unit_of_work.collection_tasks.tasks == []
    assert unit_of_work.commit_count == 0


class StubCollectionJobSubmitter:
    def __init__(self, result: SubmittedCollectionJob | Exception) -> None:
        self.result = result

    async def submit(self, **kwargs: object) -> SubmittedCollectionJob:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def build_route_app(result: SubmittedCollectionJob | Exception) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.collection_job_submitter = StubCollectionJobSubmitter(result)
    return app


def route_result() -> SubmittedCollectionJob:
    request = build_request()
    return SubmittedCollectionJob(
        job=CollectionJob(
            client_id=CLIENT_ID,
            external_request_id=request.external_request_id,
            idempotency_key_hash="sha256:" + "c" * 64,
            request_payload=request.canonical_payload(),
        ),
        created=True,
        task_count=2,
        callback_token_expires_at=None,
    )


async def post_job(
    app: FastAPI,
    *,
    headers: dict[str, str] | None = None,
) -> object:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/v1/collection-jobs",
            headers=headers,
            json=build_request().model_dump(mode="json", by_alias=True),
        )


async def test_collection_job_route_returns_202_contract_and_location() -> None:
    submitted = route_result()
    response = await post_job(
        build_route_app(submitted),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Idempotency-Key": IDEMPOTENCY_KEY,
        },
    )

    assert response.status_code == 202
    assert response.headers["location"] == (
        f"/v1/collection-jobs/{submitted.job.id}"
    )
    assert response.json() == {
        "collectionJobId": str(submitted.job.id),
        "externalRequestId": "jobreq_001",
        "status": "queued",
        "callbackTokenExpiresAt": None,
    }


async def test_invalid_callback_token_is_redacted_from_validation_response() -> None:
    from crawler_api.main import create_app

    invalid_token = "short-callback-secret"
    payload = build_request().model_dump(mode="json", by_alias=True)
    payload["callback"] = {
        "url": CALLBACK_URL,
        "accessToken": invalid_token,
        "tokenType": "Bearer",
    }
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/collection-jobs",
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
            json=payload,
        )

    assert response.status_code == 422
    assert invalid_token not in response.text
    assert "[redacted]" in response.text


@pytest.mark.parametrize(
    ("result", "headers", "expected_status"),
    [
        (route_result(), {"Idempotency-Key": IDEMPOTENCY_KEY}, 401),
        (
            route_result(),
            {"Authorization": f"Bearer {ACCESS_TOKEN}"},
            400,
        ),
        (
            CallerAccessTokenAuthenticationDenied(),
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
            401,
        ),
        (
            CallerAccessTokenInsufficientScope(),
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
            403,
        ),
        (
            CollectionJobIdempotencyConflict(),
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
            409,
        ),
        (
            CollectionJobCallbackInvalid(),
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Idempotency-Key": IDEMPOTENCY_KEY,
            },
            422,
        ),
    ],
)
async def test_collection_job_route_maps_auth_header_and_conflict_errors(
    result: SubmittedCollectionJob | Exception,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    response = await post_job(build_route_app(result), headers=headers)

    assert response.status_code == expected_status

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from crawler_api.api.v1.collection_jobs import router
from crawler_api.api.validation_errors import install_validation_error_handler
from crawler_api.application.callback_token_rotation import (
    CallbackTokenRotationConflict,
    CallbackTokenRotationInvalid,
    CallbackTokenRotationNotFound,
    CallbackTokenRotationResult,
    CollectionJobCallbackTokenRotator,
)
from crawler_api.application.caller_authentication import (
    CallerAccessTokenAuthenticationDenied,
    CallerAccessTokenAuthenticator,
    CallerAccessTokenInsufficientScope,
)
from crawler_api.domain.callbacks import (
    CollectionJobCallbackConfig,
    CollectionJobCallbackCredentialRotation,
)
from crawler_api.domain.entities import ApiAccessToken, ApiClient, CollectionJob
from crawler_api.domain.enums import CollectionState
from crawler_api.domain.webhooks import WebhookOutboxEntry, WebhookOutboxStatus
from crawler_api.infrastructure.security.callback_tokens import (
    CallbackTokenProtector,
    ProtectedCallbackToken,
)
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import (
    CollectionJobPayload,
    RotateCollectionJobCallbackTokenRequest,
)
from crawler_contracts.common import ApiScope
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

CLIENT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_CLIENT_ID = UUID("99999999-9999-4999-8999-999999999999")
TOKEN_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
CALLER_TOKEN = "cat_callback_rotation_caller_token_123456789012"
NEW_CALLBACK_TOKEN = "callback_rotation_token_123456789012345678901234"
SECOND_CALLBACK_TOKEN = "callback_rotation_token_987654321098765432109876"
TOKEN_HASH_KEY = "api-token-hash-key-that-is-at-least-32-bytes"
CALLBACK_ENCRYPTION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
CALLBACK_FINGERPRINT_KEY = "callback-fingerprint-key-that-is-at-least-32-bytes"


class FakeApiClientRepository:
    def __init__(self, client: ApiClient) -> None:
        self.client = client

    async def get_for_share(self, client_id: UUID) -> ApiClient | None:
        return self.client if self.client.id == client_id else None


class FakeApiAccessTokenRepository:
    def __init__(self, token: ApiAccessToken) -> None:
        self.token = token

    async def get_by_token_hash(self, token_hash: str) -> ApiAccessToken | None:
        return self.token if self.token.token_hash == token_hash else None

    async def get(self, token_id: UUID) -> ApiAccessToken | None:
        return self.token if self.token.id == token_id else None

    async def update_last_used(self, token_id: UUID, last_used_at: datetime) -> None:
        assert token_id == self.token.id
        self.token.last_used_at = last_used_at


class FakeCollectionJobRepository:
    def __init__(self, job: CollectionJob | None) -> None:
        self.job = job
        self.locked: list[tuple[UUID, UUID]] = []

    async def get_for_client_for_update(
        self, job_id: UUID, client_id: UUID
    ) -> CollectionJob | None:
        self.locked.append((job_id, client_id))
        if self.job is not None and self.job.id == job_id and self.job.client_id == client_id:
            return self.job
        return None


class FakeCallbackConfigRepository:
    def __init__(self, config: CollectionJobCallbackConfig | None) -> None:
        self.config = config

    async def get_for_job(self, job_id: UUID) -> CollectionJobCallbackConfig | None:
        if self.config is not None and self.config.collection_job_id == job_id:
            return self.config
        return None

    async def update_credentials(
        self,
        config: CollectionJobCallbackConfig,
        *,
        expected_credential_version: int,
        updated_at: datetime,
    ) -> bool:
        if (
            self.config is None
            or self.config.id != config.id
            or self.config.credential_version != expected_credential_version
        ):
            return False
        assert config.updated_at == updated_at
        self.config = config
        return True


class FakeRotationRepository:
    def __init__(self) -> None:
        self.rotations: dict[tuple[UUID, str], CollectionJobCallbackCredentialRotation] = {}

    async def add(self, rotation: CollectionJobCallbackCredentialRotation) -> None:
        key = (rotation.callback_config_id, rotation.idempotency_key_hash)
        assert key not in self.rotations
        self.rotations[key] = rotation

    async def get(
        self, callback_config_id: UUID, idempotency_key_hash: str
    ) -> CollectionJobCallbackCredentialRotation | None:
        return self.rotations.get((callback_config_id, idempotency_key_hash))


class FakeWebhookOutboxRepository:
    def __init__(self, entries: list[WebhookOutboxEntry] | None = None) -> None:
        self.entries = entries or []

    async def has_undelivered_for_callback(self, callback_config_id: UUID) -> bool:
        return any(
            entry.callback_config_id == callback_config_id and entry.delivered_at is None
            for entry in self.entries
        )

    async def resume_credential_blocked(
        self, callback_config_id: UUID, resumed_at: datetime
    ) -> int:
        resumed = 0
        updated: list[WebhookOutboxEntry] = []
        for entry in self.entries:
            if (
                entry.callback_config_id == callback_config_id
                and entry.status is WebhookOutboxStatus.CREDENTIAL_BLOCKED
                and entry.delivered_at is None
            ):
                entry = replace(
                    entry,
                    status=WebhookOutboxStatus.PENDING,
                    next_attempt_at=resumed_at,
                    last_failure_code=None,
                )
                resumed += 1
            updated.append(entry)
        self.entries = updated
        return resumed


class FakeUnitOfWork:
    def __init__(
        self,
        *,
        client: ApiClient,
        token: ApiAccessToken,
        job: CollectionJob | None,
        config: CollectionJobCallbackConfig | None,
        entries: list[WebhookOutboxEntry] | None = None,
    ) -> None:
        self.api_clients = FakeApiClientRepository(client)
        self.api_access_tokens = FakeApiAccessTokenRepository(token)
        self.collection_jobs = FakeCollectionJobRepository(job)
        self.callback_configs = FakeCallbackConfigRepository(config)
        self.callback_credential_rotations = FakeRotationRepository()
        self.webhook_outbox = FakeWebhookOutboxRepository(entries)
        self.commit_count = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


def token_hasher() -> AccessTokenHasher:
    return AccessTokenHasher(SecretStr(TOKEN_HASH_KEY))


def callback_protector() -> CallbackTokenProtector:
    return CallbackTokenProtector(
        encryption_key=SecretStr(CALLBACK_ENCRYPTION_KEY),
        fingerprint_key=SecretStr(CALLBACK_FINGERPRINT_KEY),
        encryption_key_version="v1",
    )


def build_client() -> ApiClient:
    return ApiClient(
        id=CLIENT_ID,
        tenant_id="rotation-tenant",
        display_name="Rotation Tenant",
        allowed_scopes=list(ApiScope),
        config_fingerprint="sha256:" + "a" * 64,
        status="active",
    )


def build_caller_token(*, scopes: list[ApiScope] | None = None) -> ApiAccessToken:
    return ApiAccessToken(
        id=TOKEN_ID,
        client_id=CLIENT_ID,
        token_hash=token_hasher().hash(CALLER_TOKEN),
        issued_via_key_fingerprint="sha256:" + "b" * 64,
        scopes=scopes or list(ApiScope),
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )


def build_job(
    *,
    client_id: UUID = CLIENT_ID,
    status: CollectionState = CollectionState.RUNNING,
) -> CollectionJob:
    return CollectionJob(
        client_id=client_id,
        external_request_id="jobreq_rotation_001",
        idempotency_key_hash="sha256:" + "c" * 64,
        request_payload=CollectionJobPayload.model_validate(
            {
                "schemaVersion": 1,
                "source": "zhaopin",
                "queries": [{"keyword": "Python", "region": "Shanghai"}],
            }
        ),
        status=status,
    )


def build_config(job: CollectionJob) -> CollectionJobCallbackConfig:
    config_id = uuid4()
    protector = callback_protector()
    protected = protector.protect(
        SecretStr("initial_callback_token_123456789012345678901234"),
        callback_config_id=config_id,
        collection_job_id=job.id,
        credential_version=1,
    )
    return CollectionJobCallbackConfig(
        id=config_id,
        collection_job_id=job.id,
        target_url="http://upstream.internal/api/job-collection/events",
        token_ciphertext=protected.ciphertext,
        token_nonce=protected.nonce,
        token_fingerprint=protected.fingerprint,
        token_expires_at=NOW + timedelta(days=1),
        encryption_key_version=protected.encryption_key_version,
    )


def build_unit_of_work(
    *,
    status: CollectionState = CollectionState.RUNNING,
    entries: list[WebhookOutboxEntry] | None = None,
    client_id: UUID = CLIENT_ID,
) -> FakeUnitOfWork:
    job = build_job(client_id=client_id, status=status)
    return FakeUnitOfWork(
        client=build_client(),
        token=build_caller_token(),
        job=job,
        config=build_config(job),
        entries=entries,
    )


def build_rotator(unit_of_work: FakeUnitOfWork) -> CollectionJobCallbackTokenRotator:
    return CollectionJobCallbackTokenRotator(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=CallerAccessTokenAuthenticator(token_hasher().hash),
        callback_token_protector=callback_protector(),
        clock=lambda: NOW,
    )


def rotation_request(
    *,
    token: str = NEW_CALLBACK_TOKEN,
    expires_at: datetime | None = None,
) -> RotateCollectionJobCallbackTokenRequest:
    return RotateCollectionJobCallbackTokenRequest(
        accessToken=token,
        tokenType="Bearer",
        expiresAt=expires_at,
    )


async def rotate(
    rotator: CollectionJobCallbackTokenRotator,
    job_id: UUID,
    *,
    key: str = "rotate-operation-001",
    request: RotateCollectionJobCallbackTokenRequest | None = None,
) -> CallbackTokenRotationResult:
    return await rotator.rotate(
        access_token=SecretStr(CALLER_TOKEN),
        idempotency_key=SecretStr(key),
        job_id=job_id,
        request=request or rotation_request(),
    )


async def test_rotation_reencrypts_increments_version_and_resumes_only_blocked() -> None:
    unit_of_work = build_unit_of_work()
    job = unit_of_work.collection_jobs.job
    original = unit_of_work.callback_configs.config
    assert job is not None and original is not None
    unit_of_work.webhook_outbox.entries = [
        WebhookOutboxEntry(
            event_id=uuid4(),
            callback_config_id=original.id,
            status=WebhookOutboxStatus.CREDENTIAL_BLOCKED,
            next_attempt_at=NOW - timedelta(minutes=5),
            last_failure_code="http_401",
        ),
        WebhookOutboxEntry(
            event_id=uuid4(),
            callback_config_id=original.id,
            status=WebhookOutboxStatus.PERMANENTLY_FAILED,
            next_attempt_at=NOW - timedelta(minutes=5),
            last_failure_code="http_400",
        ),
    ]

    result = await rotate(build_rotator(unit_of_work), job.id)

    stored = unit_of_work.callback_configs.config
    assert stored is not None
    assert result.callback_token_expires_at == NOW + timedelta(days=7)
    assert result.credential_version == 2
    assert stored.target_url == original.target_url
    assert stored.credential_version == 2
    assert stored.token_ciphertext != original.token_ciphertext
    assert stored.token_nonce != original.token_nonce
    assert stored.token_fingerprint != original.token_fingerprint
    assert stored.updated_at == NOW
    decrypted = callback_protector().decrypt(
        ProtectedCallbackToken(
            ciphertext=stored.token_ciphertext,
            nonce=stored.token_nonce,
            fingerprint=stored.token_fingerprint,
            encryption_key_version=stored.encryption_key_version,
        ),
        callback_config_id=stored.id,
        collection_job_id=job.id,
        credential_version=2,
    )
    assert decrypted.get_secret_value() == NEW_CALLBACK_TOKEN
    assert len(unit_of_work.callback_credential_rotations.rotations) == 1
    assert NEW_CALLBACK_TOKEN not in repr(unit_of_work.callback_credential_rotations.rotations)
    blocked, permanent = unit_of_work.webhook_outbox.entries
    assert blocked.status is WebhookOutboxStatus.PENDING
    assert blocked.next_attempt_at == NOW
    assert blocked.last_failure_code is None
    assert permanent.status is WebhookOutboxStatus.PERMANENTLY_FAILED
    assert unit_of_work.commit_count == 1


async def test_rotation_history_supports_replay_new_rotation_and_old_replay() -> None:
    unit_of_work = build_unit_of_work()
    job = unit_of_work.collection_jobs.job
    assert job is not None
    rotator = build_rotator(unit_of_work)

    first = await rotate(rotator, job.id)
    replay = await rotate(rotator, job.id)
    second = await rotate(
        rotator,
        job.id,
        key="rotate-operation-002",
        request=rotation_request(token=SECOND_CALLBACK_TOKEN),
    )
    old_replay = await rotate(rotator, job.id)

    assert replay == first
    assert first.credential_version == 2
    assert second.credential_version == 3
    assert old_replay == first
    assert unit_of_work.callback_configs.config is not None
    assert unit_of_work.callback_configs.config.credential_version == 3
    assert len(unit_of_work.callback_credential_rotations.rotations) == 2
    assert unit_of_work.commit_count == 4


@pytest.mark.parametrize(
    "changed_request",
    [
        rotation_request(token=SECOND_CALLBACK_TOKEN),
        rotation_request(expires_at=NOW + timedelta(days=2)),
    ],
)
async def test_reused_rotation_key_with_changed_request_conflicts(
    changed_request: RotateCollectionJobCallbackTokenRequest,
) -> None:
    unit_of_work = build_unit_of_work()
    job = unit_of_work.collection_jobs.job
    assert job is not None
    rotator = build_rotator(unit_of_work)
    await rotate(rotator, job.id)

    with pytest.raises(CallbackTokenRotationConflict):
        await rotate(rotator, job.id, request=changed_request)


async def test_terminal_job_requires_an_undelivered_outbox_entry() -> None:
    unit_of_work = build_unit_of_work(status=CollectionState.SUCCEEDED)
    job = unit_of_work.collection_jobs.job
    config = unit_of_work.callback_configs.config
    assert job is not None and config is not None

    with pytest.raises(CallbackTokenRotationConflict):
        await rotate(build_rotator(unit_of_work), job.id)

    unit_of_work.webhook_outbox.entries.append(
        WebhookOutboxEntry(
            event_id=uuid4(),
            callback_config_id=config.id,
            next_attempt_at=NOW,
        )
    )
    result = await rotate(build_rotator(unit_of_work), job.id)
    assert result.credential_version == 2


async def test_destroyed_credential_cannot_be_rotated() -> None:
    unit_of_work = build_unit_of_work()
    job = unit_of_work.collection_jobs.job
    stored = unit_of_work.callback_configs.config
    assert job is not None and stored is not None
    unit_of_work.callback_configs.config = replace(
        stored,
        token_ciphertext=None,
        token_nonce=None,
        credential_destroyed_at=NOW - timedelta(seconds=1),
    )

    with pytest.raises(CallbackTokenRotationConflict):
        await rotate(build_rotator(unit_of_work), job.id)


async def test_successful_rotation_replays_after_credential_destruction() -> None:
    unit_of_work = build_unit_of_work()
    job = unit_of_work.collection_jobs.job
    assert job is not None
    rotator = build_rotator(unit_of_work)
    original_result = await rotate(rotator, job.id)
    stored = unit_of_work.callback_configs.config
    assert stored is not None
    unit_of_work.callback_configs.config = replace(
        stored,
        token_ciphertext=None,
        token_nonce=None,
        credential_destroyed_at=NOW + timedelta(seconds=1),
    )

    replayed = await rotate(rotator, job.id)

    assert replayed == original_result
    assert len(unit_of_work.callback_credential_rotations.rotations) == 1


@pytest.mark.parametrize(
    "rotation_payload",
    [
        rotation_request(token=CALLER_TOKEN),
        rotation_request(expires_at=NOW),
        rotation_request(expires_at=NOW + timedelta(days=7, seconds=1)),
    ],
)
async def test_invalid_rotation_credential_is_rejected(
    rotation_payload: RotateCollectionJobCallbackTokenRequest,
) -> None:
    unit_of_work = build_unit_of_work()
    job = unit_of_work.collection_jobs.job
    assert job is not None
    with pytest.raises(CallbackTokenRotationInvalid):
        await rotate(build_rotator(unit_of_work), job.id, request=rotation_payload)


async def test_cross_tenant_job_and_missing_callback_are_not_found() -> None:
    cross_tenant = build_unit_of_work(client_id=OTHER_CLIENT_ID)
    cross_job = cross_tenant.collection_jobs.job
    assert cross_job is not None
    with pytest.raises(CallbackTokenRotationNotFound):
        await rotate(build_rotator(cross_tenant), cross_job.id)

    missing_callback = build_unit_of_work()
    job = missing_callback.collection_jobs.job
    assert job is not None
    missing_callback.callback_configs.config = None
    with pytest.raises(CallbackTokenRotationNotFound):
        await rotate(build_rotator(missing_callback), job.id)


def test_rotation_contract_rejects_url_and_naive_expiry() -> None:
    with pytest.raises(ValidationError):
        RotateCollectionJobCallbackTokenRequest.model_validate(
            {
                "accessToken": NEW_CALLBACK_TOKEN,
                "tokenType": "Bearer",
                "url": "http://upstream.internal/changed",
            }
        )
    with pytest.raises(ValidationError):
        rotation_request(expires_at=datetime(2026, 9, 18, 9, 0))


class StubRotator:
    def __init__(self, result: CallbackTokenRotationResult | Exception) -> None:
        self.result = result

    async def rotate(self, **kwargs: object) -> CallbackTokenRotationResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def route_app(result: CallbackTokenRotationResult | Exception) -> FastAPI:
    app = FastAPI()
    install_validation_error_handler(app)
    app.include_router(router)
    app.state.collection_job_callback_token_rotator = StubRotator(result)
    return app


async def put_rotation(
    app: FastAPI,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> object:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.put(
            f"/v1/collection-jobs/{uuid4()}/callback-token",
            headers=headers,
            json=payload
            or {
                "accessToken": NEW_CALLBACK_TOKEN,
                "tokenType": "Bearer",
                "expiresAt": None,
            },
        )


async def test_rotation_route_returns_frozen_response_and_rejects_url() -> None:
    result = CallbackTokenRotationResult(
        callback_token_expires_at=NOW + timedelta(days=7),
        credential_version=2,
    )
    headers = {
        "Authorization": f"Bearer {CALLER_TOKEN}",
        "Idempotency-Key": "rotate-operation-001",
    }
    response = await put_rotation(route_app(result), headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "callbackTokenExpiresAt": "2026-09-24T09:00:00Z",
        "credentialVersion": 2,
    }

    invalid = await put_rotation(
        route_app(result),
        headers=headers,
        payload={
            "accessToken": NEW_CALLBACK_TOKEN,
            "tokenType": "Bearer",
            "url": "http://upstream.internal/changed",
        },
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    ("result", "headers", "expected_status"),
    [
        (
            CallbackTokenRotationResult(NOW + timedelta(days=7), 2),
            {"Idempotency-Key": "rotate-operation-001"},
            401,
        ),
        (
            CallbackTokenRotationResult(NOW + timedelta(days=7), 2),
            {"Authorization": f"Bearer {CALLER_TOKEN}"},
            400,
        ),
        (
            CallerAccessTokenAuthenticationDenied(),
            {
                "Authorization": f"Bearer {CALLER_TOKEN}",
                "Idempotency-Key": "rotate-operation-001",
            },
            401,
        ),
        (
            CallerAccessTokenInsufficientScope(),
            {
                "Authorization": f"Bearer {CALLER_TOKEN}",
                "Idempotency-Key": "rotate-operation-001",
            },
            403,
        ),
        (
            CallbackTokenRotationNotFound(),
            {
                "Authorization": f"Bearer {CALLER_TOKEN}",
                "Idempotency-Key": "rotate-operation-001",
            },
            404,
        ),
        (
            CallbackTokenRotationConflict(),
            {
                "Authorization": f"Bearer {CALLER_TOKEN}",
                "Idempotency-Key": "rotate-operation-001",
            },
            409,
        ),
        (
            CallbackTokenRotationInvalid(),
            {
                "Authorization": f"Bearer {CALLER_TOKEN}",
                "Idempotency-Key": "rotate-operation-001",
            },
            422,
        ),
    ],
)
async def test_rotation_route_maps_errors(
    result: CallbackTokenRotationResult | Exception,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    response = await put_rotation(route_app(result), headers=headers)
    assert response.status_code == expected_status


async def test_invalid_rotation_token_is_redacted() -> None:
    result = CallbackTokenRotationResult(
        callback_token_expires_at=NOW + timedelta(days=7),
        credential_version=2,
    )
    secret = "short-sensitive-token"
    response = await put_rotation(
        route_app(result),
        headers={
            "Authorization": f"Bearer {CALLER_TOKEN}",
            "Idempotency-Key": "rotate-operation-001",
        },
        payload={"accessToken": secret, "tokenType": "Bearer"},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "[redacted]" in response.text

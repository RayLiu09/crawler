import hashlib
import hmac
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from crawler_api.domain.callbacks import CollectionJobCallbackConfig
from crawler_api.domain.entities import CollectionJob, CollectionTask
from crawler_api.domain.enums import (
    TERMINAL_COLLECTION_STATES,
    CollectionState,
    DesiredState,
)
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_contracts.collection_jobs import (
    CollectionJobCallback,
    CollectionJobPayload,
    SubmitCollectionJobRequest,
)
from crawler_contracts.common import ApiScope
from pydantic import SecretStr

from .caller_authentication import CallerAccessTokenAuthenticator
from .collection_events import (
    accepted_event,
    add_event_with_outbox,
    desired_state_changed_event,
    terminal_event,
)
from .ports import UnitOfWork
from .tenant_allowlist import TenantAllowlist

CALLBACK_TOKEN_TTL = timedelta(days=7)


class InvalidIdempotencyKey(ValueError):
    pass


class CollectionJobIdempotencyConflict(Exception):
    pass


class CollectionJobNotFound(Exception):
    pass


class CollectionJobControlConflict(Exception):
    pass


class CollectionJobCallbackInvalid(Exception):
    pass


class CollectionJobControlAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class SubmittedCollectionJob:
    job: CollectionJob
    created: bool
    task_count: int
    callback_token_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CollectionJobDetails:
    job: CollectionJob
    task_status_counts: dict[CollectionState, int]
    processed_count: int
    failure_code_counts: dict[str, int]


def hash_idempotency_key(idempotency_key: SecretStr) -> str:
    digest = hashlib.sha256(idempotency_key.get_secret_value().encode()).hexdigest()
    return f"sha256:{digest}"


class CollectionJobSubmitter:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: CallerAccessTokenAuthenticator,
        tenant_allowlist: TenantAllowlist,
        callback_token_protector: CallbackTokenProtector,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._tenant_allowlist = tenant_allowlist
        self._callback_token_protector = callback_token_protector
        self._clock = clock

    async def submit(
        self,
        *,
        access_token: SecretStr,
        idempotency_key: SecretStr,
        request: SubmitCollectionJobRequest,
    ) -> SubmittedCollectionJob:
        raw_idempotency_key = idempotency_key.get_secret_value()
        if not raw_idempotency_key or len(raw_idempotency_key) > 200:
            raise InvalidIdempotencyKey

        submitted_at = self._clock()
        key_hash = hash_idempotency_key(idempotency_key)
        payload = request.canonical_payload()

        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_WRITE,
                authenticated_at=submitted_at,
            )
            client_id = caller.client.id
            callback_expires_at = self._validate_callback_request(
                tenant_id=caller.client.tenant_id,
                caller_access_token=access_token,
                callback=request.callback,
                submitted_at=submitted_at,
            )

            existing_by_key = await unit_of_work.collection_jobs.get_by_idempotency_hash(
                client_id, key_hash
            )
            if existing_by_key is not None:
                result = await self._resolve_replay(
                    unit_of_work, existing_by_key, request
                )
                await unit_of_work.commit()
                return result

            existing_by_request = await unit_of_work.collection_jobs.get_by_external_request_id(
                client_id, request.external_request_id
            )
            if existing_by_request is not None:
                result = await self._resolve_external_request_replay(
                    unit_of_work, existing_by_request, request, payload
                )
                await unit_of_work.commit()
                return result

            job = CollectionJob(
                client_id=client_id,
                external_request_id=request.external_request_id,
                idempotency_key_hash=key_hash,
                request_payload=payload,
            )
            inserted = await unit_of_work.collection_jobs.try_add(job)
            if not inserted:
                concurrent = await self._find_concurrent_winner(
                    unit_of_work, client_id, key_hash, request
                )
                await unit_of_work.commit()
                return concurrent

            callback_config: CollectionJobCallbackConfig | None = None
            if request.callback is not None:
                if callback_expires_at is None:
                    raise RuntimeError("validated callback is missing an expiry")
                callback_config = self._build_callback_config(
                    job=job,
                    callback=request.callback,
                    token_expires_at=callback_expires_at,
                )
                await unit_of_work.callback_configs.add(callback_config)

            for query in payload.queries:
                await unit_of_work.collection_tasks.add(
                    CollectionTask(
                        collection_job_id=job.id,
                        source=payload.source,
                        keyword=query.keyword,
                        region=query.region,
                        page_limit=query.page_limit,
                    )
                )
            await add_event_with_outbox(
                unit_of_work,
                accepted_event(job, len(payload.queries), submitted_at),
                callback_config_id=(
                    callback_config.id if callback_config is not None else None
                ),
            )
            await unit_of_work.commit()

        return SubmittedCollectionJob(
            job=job,
            created=True,
            task_count=len(payload.queries),
            callback_token_expires_at=callback_expires_at,
        )

    async def _resolve_replay(
        self,
        unit_of_work: UnitOfWork,
        existing: CollectionJob,
        request: SubmitCollectionJobRequest,
    ) -> SubmittedCollectionJob:
        if (
            existing.external_request_id != request.external_request_id
            or existing.request_payload != request.canonical_payload()
        ):
            raise CollectionJobIdempotencyConflict
        return await self._resolve_callback_replay(unit_of_work, existing, request.callback)

    async def _resolve_external_request_replay(
        self,
        unit_of_work: UnitOfWork,
        existing: CollectionJob,
        request: SubmitCollectionJobRequest,
        payload: CollectionJobPayload,
    ) -> SubmittedCollectionJob:
        if existing.request_payload != payload:
            raise CollectionJobIdempotencyConflict
        return await self._resolve_callback_replay(unit_of_work, existing, request.callback)

    async def _find_concurrent_winner(
        self,
        unit_of_work: UnitOfWork,
        client_id: UUID,
        key_hash: str,
        request: SubmitCollectionJobRequest,
    ) -> SubmittedCollectionJob:
        existing_by_key = await unit_of_work.collection_jobs.get_by_idempotency_hash(
            client_id, key_hash
        )
        if existing_by_key is not None:
            return await self._resolve_replay(unit_of_work, existing_by_key, request)
        existing_by_request = await unit_of_work.collection_jobs.get_by_external_request_id(
            client_id, request.external_request_id
        )
        if existing_by_request is not None:
            return await self._resolve_external_request_replay(
                unit_of_work,
                existing_by_request,
                request,
                request.canonical_payload(),
            )
        raise RuntimeError("collection job insert conflicted without a visible winner")

    def _validate_callback_request(
        self,
        *,
        tenant_id: str,
        caller_access_token: SecretStr,
        callback: CollectionJobCallback | None,
        submitted_at: datetime,
    ) -> datetime | None:
        if callback is None:
            return None
        tenant = self._tenant_allowlist.get_enabled_tenant(tenant_id)
        if tenant is None or not tenant.allows_callback_url(callback.url):
            raise CollectionJobCallbackInvalid
        if hmac.compare_digest(
            caller_access_token.get_secret_value(),
            callback.access_token.get_secret_value(),
        ):
            raise CollectionJobCallbackInvalid
        expires_at = callback.expires_at or (submitted_at + CALLBACK_TOKEN_TTL)
        if expires_at <= submitted_at or expires_at > submitted_at + CALLBACK_TOKEN_TTL:
            raise CollectionJobCallbackInvalid
        return expires_at

    def _build_callback_config(
        self,
        *,
        job: CollectionJob,
        callback: CollectionJobCallback,
        token_expires_at: datetime,
    ) -> CollectionJobCallbackConfig:
        callback_config_id = uuid4()
        credential_version = 1
        protected = self._callback_token_protector.protect(
            callback.access_token,
            callback_config_id=callback_config_id,
            collection_job_id=job.id,
            credential_version=credential_version,
        )
        return CollectionJobCallbackConfig(
            id=callback_config_id,
            collection_job_id=job.id,
            target_url=str(callback.url),
            auth_scheme="bearer",
            token_ciphertext=protected.ciphertext,
            token_nonce=protected.nonce,
            token_fingerprint=protected.fingerprint,
            token_expires_at=token_expires_at,
            credential_version=credential_version,
            encryption_key_version=protected.encryption_key_version,
        )

    async def _resolve_callback_replay(
        self,
        unit_of_work: UnitOfWork,
        existing: CollectionJob,
        callback: CollectionJobCallback | None,
    ) -> SubmittedCollectionJob:
        stored = await unit_of_work.callback_configs.get_for_job(existing.id)
        if callback is None:
            if stored is not None:
                raise CollectionJobIdempotencyConflict
            return SubmittedCollectionJob(job=existing, created=False, task_count=0)
        if (
            stored is None
            or stored.target_url != str(callback.url)
            or stored.auth_scheme != "bearer"
            or not hmac.compare_digest(
                stored.token_fingerprint,
                self._callback_token_protector.fingerprint(callback.access_token),
            )
            or (
                callback.expires_at is not None
                and stored.token_expires_at != callback.expires_at
            )
        ):
            raise CollectionJobIdempotencyConflict
        return SubmittedCollectionJob(
            job=existing,
            created=False,
            task_count=0,
            callback_token_expires_at=stored.token_expires_at,
        )


class CollectionJobReader:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: CallerAccessTokenAuthenticator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._clock = clock

    async def get(self, *, access_token: SecretStr, job_id: UUID) -> CollectionJobDetails:
        read_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_READ,
                authenticated_at=read_at,
            )
            job = await unit_of_work.collection_jobs.get_for_client(job_id, caller.client.id)
            if job is None:
                raise CollectionJobNotFound
            tasks = await unit_of_work.collection_tasks.list_for_job(job.id)
            result = summarize_job(job, tasks)
            await unit_of_work.commit()
            return result


class CollectionJobController:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: CallerAccessTokenAuthenticator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._clock = clock

    async def control(
        self,
        *,
        access_token: SecretStr,
        job_id: UUID,
        action: CollectionJobControlAction,
    ) -> CollectionJobDetails:
        controlled_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_CONTROL,
                authenticated_at=controlled_at,
            )
            job = await unit_of_work.collection_jobs.get_for_client_for_update(
                job_id, caller.client.id
            )
            if job is None:
                raise CollectionJobNotFound
            tasks = await unit_of_work.collection_tasks.list_for_job_for_update(job.id)
            original_task_states = {task.id: (task.desired_state, task.status) for task in tasks}

            changed = self._apply(action, job, tasks, controlled_at)
            if changed:
                await unit_of_work.collection_jobs.update_control_state(job, controlled_at)
                for task in tasks:
                    if original_task_states[task.id] != (
                        task.desired_state,
                        task.status,
                    ):
                        await unit_of_work.collection_tasks.update_control_state(
                            task, controlled_at
                        )
                await add_event_with_outbox(
                    unit_of_work,
                    desired_state_changed_event(job, action.value, controlled_at),
                )
                if job.status in TERMINAL_COLLECTION_STATES:
                    await add_event_with_outbox(
                        unit_of_work,
                        terminal_event(job, tasks, controlled_at),
                    )

            result = summarize_job(job, tasks)
            await unit_of_work.commit()
            return result

    def _apply(
        self,
        action: CollectionJobControlAction,
        job: CollectionJob,
        tasks: list[CollectionTask],
        controlled_at: datetime,
    ) -> bool:
        if action is CollectionJobControlAction.PAUSE:
            return self._pause(job, tasks, controlled_at)
        if action is CollectionJobControlAction.RESUME:
            return self._resume(job, tasks, controlled_at)
        return self._cancel(job, tasks, controlled_at)

    def _pause(
        self,
        job: CollectionJob,
        tasks: list[CollectionTask],
        controlled_at: datetime,
    ) -> bool:
        if job.desired_state is DesiredState.PAUSED:
            return False
        self._reject_terminal_or_cancelled(job)
        if any(task.status is CollectionState.CANCELLING for task in tasks):
            raise CollectionJobControlConflict

        transitions = {
            CollectionState.QUEUED: CollectionState.PAUSED,
            CollectionState.RUNNING: CollectionState.PAUSING,
            CollectionState.AWAITING_HUMAN: CollectionState.PAUSING,
        }
        self._update_tasks(tasks, DesiredState.PAUSED, transitions)
        job.desired_state = DesiredState.PAUSED
        job.status = (
            CollectionState.PAUSING
            if any(task.status is CollectionState.PAUSING for task in tasks)
            else CollectionState.PAUSED
        )
        self._mark_changed(job, controlled_at)
        return True

    def _resume(
        self,
        job: CollectionJob,
        tasks: list[CollectionTask],
        controlled_at: datetime,
    ) -> bool:
        if job.status in TERMINAL_COLLECTION_STATES:
            raise CollectionJobControlConflict
        if job.desired_state is DesiredState.RUNNING:
            return False
        if job.desired_state is DesiredState.CANCELLED or job.status is not CollectionState.PAUSED:
            raise CollectionJobControlConflict

        self._update_tasks(
            tasks,
            DesiredState.RUNNING,
            {CollectionState.PAUSED: CollectionState.QUEUED},
        )
        job.desired_state = DesiredState.RUNNING
        job.status = CollectionState.QUEUED
        job.completed_at = None
        self._mark_changed(job, controlled_at)
        return True

    def _cancel(
        self,
        job: CollectionJob,
        tasks: list[CollectionTask],
        controlled_at: datetime,
    ) -> bool:
        if job.desired_state is DesiredState.CANCELLED:
            return False
        self._reject_terminal_or_cancelled(job)

        transitions = {
            CollectionState.QUEUED: CollectionState.CANCELLED,
            CollectionState.PAUSED: CollectionState.CANCELLED,
            CollectionState.RUNNING: CollectionState.CANCELLING,
            CollectionState.PAUSING: CollectionState.CANCELLING,
            CollectionState.AWAITING_HUMAN: CollectionState.CANCELLING,
        }
        self._update_tasks(tasks, DesiredState.CANCELLED, transitions)
        job.desired_state = DesiredState.CANCELLED
        if any(task.status is CollectionState.CANCELLING for task in tasks):
            job.status = CollectionState.CANCELLING
        else:
            job.status = CollectionState.CANCELLED
            job.completed_at = controlled_at
        self._mark_changed(job, controlled_at)
        return True

    @staticmethod
    def _update_tasks(
        tasks: list[CollectionTask],
        desired_state: DesiredState,
        transitions: dict[CollectionState, CollectionState],
    ) -> None:
        for task in tasks:
            if task.status in TERMINAL_COLLECTION_STATES:
                continue
            task.desired_state = desired_state
            task.status = transitions.get(task.status, task.status)

    @staticmethod
    def _reject_terminal_or_cancelled(job: CollectionJob) -> None:
        if (
            job.desired_state is DesiredState.CANCELLED
            or job.status in TERMINAL_COLLECTION_STATES
        ):
            raise CollectionJobControlConflict

    @staticmethod
    def _mark_changed(job: CollectionJob, controlled_at: datetime) -> None:
        job.version += 1
        job.updated_at = controlled_at


def summarize_job(job: CollectionJob, tasks: list[CollectionTask]) -> CollectionJobDetails:
    status_counts = Counter(task.status for task in tasks)
    failure_counts = Counter(task.failure_code for task in tasks if task.failure_code is not None)
    return CollectionJobDetails(
        job=job,
        task_status_counts=dict(status_counts),
        processed_count=sum(task.processed_count for task in tasks),
        failure_code_counts=dict(failure_counts),
    )

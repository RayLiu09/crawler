from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from crawler_api.domain.entities import (
    BrowserProfile,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.enums import (
    TERMINAL_COLLECTION_STATES,
    CollectionState,
    DesiredState,
)
from crawler_api.domain.task_attempts import TaskAttemptOutcome
from crawler_contracts.agent_protocol import (
    CompleteTaskRequest,
    LeaseReference,
    ReleaseTaskRequest,
    TaskCheckpointRequest,
    TaskControlResponse,
)
from pydantic import SecretStr

from .collection_events import add_event_with_outbox, terminal_event
from .ports import UnitOfWork
from .task_leases import (
    ACTIVE_LEASE_STATES,
    ActiveTaskLeaseAuthenticator,
    AuthenticatedTaskLease,
)


class TaskRuntimeStateConflict(Exception):
    """The requested checkpoint or terminal transition is not currently valid."""


class TaskRuntimeInvariantViolation(RuntimeError):
    """Persisted lease state is internally inconsistent and cannot be recovered."""


class AgentTaskRuntime:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        lease_authenticator: ActiveTaskLeaseAuthenticator,
        lease_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._lease_authenticator = lease_authenticator
        self._lease_seconds = lease_seconds
        self._clock = clock

    async def heartbeat(
        self,
        *,
        session_token: SecretStr,
        task_id: UUID,
        request: LeaseReference,
    ) -> TaskControlResponse:
        return await self._renew(
            session_token=session_token,
            task_id=task_id,
            request=request,
            checkpoint=None,
        )

    async def checkpoint(
        self,
        *,
        session_token: SecretStr,
        task_id: UUID,
        request: TaskCheckpointRequest,
    ) -> TaskControlResponse:
        return await self._renew(
            session_token=session_token,
            task_id=task_id,
            request=request,
            checkpoint=request,
        )

    async def _renew(
        self,
        *,
        session_token: SecretStr,
        task_id: UUID,
        request: LeaseReference,
        checkpoint: TaskCheckpointRequest | None,
    ) -> TaskControlResponse:
        renewed_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            lease = await self._lease_authenticator.authenticate(
                unit_of_work=unit_of_work,
                session_token=session_token,
                expected_task_id=task_id,
                lease_reference=request,
                authenticated_at=renewed_at,
            )
            task = lease.task
            if checkpoint is not None:
                if (
                    checkpoint.page_checkpoint < task.page_checkpoint
                    or checkpoint.page_checkpoint > task.page_limit
                    or checkpoint.processed_count < task.processed_count
                ):
                    raise TaskRuntimeStateConflict
                task.page_checkpoint = checkpoint.page_checkpoint
                task.processed_count = checkpoint.processed_count

            task.lease_expires_at = renewed_at + timedelta(seconds=self._lease_seconds)
            await unit_of_work.collection_tasks.update_runtime_state(task, renewed_at)
            response = TaskControlResponse(
                desired_state=task.desired_state.value,
                lease_expires_at=task.lease_expires_at,
            )
            await unit_of_work.commit()
            return response

    async def complete(
        self,
        *,
        session_token: SecretStr,
        task_id: UUID,
        request: CompleteTaskRequest,
    ) -> None:
        completed_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            lease = await self._lease_authenticator.authenticate(
                unit_of_work=unit_of_work,
                session_token=session_token,
                expected_task_id=task_id,
                lease_reference=request,
                authenticated_at=completed_at,
            )
            task_state, attempt_outcome, failure_code = self._completion_outcome(lease, request)
            await self._finish(
                unit_of_work=unit_of_work,
                lease=lease,
                task_state=task_state,
                attempt_outcome=attempt_outcome,
                failure_code=failure_code,
                finished_at=completed_at,
            )
            await unit_of_work.commit()

    async def release(
        self,
        *,
        session_token: SecretStr,
        task_id: UUID,
        request: ReleaseTaskRequest,
    ) -> None:
        released_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            lease = await self._lease_authenticator.authenticate(
                unit_of_work=unit_of_work,
                session_token=session_token,
                expected_task_id=task_id,
                lease_reference=request,
                authenticated_at=released_at,
            )
            if lease.task.desired_state is DesiredState.PAUSED:
                state = CollectionState.PAUSED
                outcome = TaskAttemptOutcome.PAUSED
                failure_code = None
            elif lease.task.desired_state is DesiredState.CANCELLED:
                state = CollectionState.CANCELLED
                outcome = TaskAttemptOutcome.CANCELLED
                failure_code = None
            else:
                state = CollectionState.QUEUED
                outcome = TaskAttemptOutcome.FAILED
                failure_code = request.failure_code
            await self._finish(
                unit_of_work=unit_of_work,
                lease=lease,
                task_state=state,
                attempt_outcome=outcome,
                failure_code=failure_code,
                finished_at=released_at,
            )
            await unit_of_work.commit()

    @staticmethod
    def _completion_outcome(
        lease: AuthenticatedTaskLease,
        request: CompleteTaskRequest,
    ) -> tuple[CollectionState, TaskAttemptOutcome, str | None]:
        if request.outcome == "completed":
            return CollectionState.SUCCEEDED, TaskAttemptOutcome.COMPLETED, None
        if request.outcome == "failed":
            return (
                CollectionState.FAILED,
                TaskAttemptOutcome.FAILED,
                request.failure_code or "agent_reported_failure",
            )
        if request.outcome == "paused":
            if (
                lease.job.desired_state is not DesiredState.PAUSED
                or lease.task.desired_state is not DesiredState.PAUSED
            ):
                raise TaskRuntimeStateConflict
            return CollectionState.PAUSED, TaskAttemptOutcome.PAUSED, None
        if (
            lease.job.desired_state is not DesiredState.CANCELLED
            or lease.task.desired_state is not DesiredState.CANCELLED
        ):
            raise TaskRuntimeStateConflict
        return CollectionState.CANCELLED, TaskAttemptOutcome.CANCELLED, None

    async def _finish(
        self,
        *,
        unit_of_work: UnitOfWork,
        lease: AuthenticatedTaskLease,
        task_state: CollectionState,
        attempt_outcome: TaskAttemptOutcome,
        failure_code: str | None,
        finished_at: datetime,
    ) -> None:
        task = lease.task
        task.status = task_state
        task.failure_code = failure_code
        self._clear_lease(task)
        await unit_of_work.collection_tasks.update_runtime_state(task, finished_at)
        await unit_of_work.agents.clear_current_task(lease.agent.id, task.id, finished_at)
        await self._release_profile(unit_of_work, lease.profile, finished_at)
        await unit_of_work.task_attempts.update_outcome(
            replace(
                lease.attempt,
                released_at=finished_at,
                outcome=attempt_outcome,
                failure_code=failure_code,
            )
        )
        tasks = await unit_of_work.collection_tasks.list_for_job_for_update(lease.job.id)
        await _aggregate_job(unit_of_work, lease.job, tasks, finished_at)

    @staticmethod
    def _clear_lease(task: CollectionTask) -> None:
        task.leased_agent_id = None
        task.leased_profile_id = None
        task.lease_token_hash = None
        task.lease_expires_at = None

    @staticmethod
    async def _release_profile(
        unit_of_work: UnitOfWork,
        profile: BrowserProfile,
        released_at: datetime,
    ) -> None:
        if profile.status == "busy":
            profile.status = "ready"
            profile.updated_at = released_at
            await unit_of_work.browser_profiles.upsert(profile)


class ExpiredLeaseRecoverer:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock

    async def recover_one(self) -> bool:
        recovered_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            candidate_job = await unit_of_work.collection_jobs.find_next_with_expired_lease(
                recovered_at
            )
            if candidate_job is None:
                await unit_of_work.commit()
                return False
            candidate_task = await unit_of_work.collection_tasks.find_expired_for_job(
                candidate_job.id, recovered_at
            )
            if candidate_task is None or candidate_task.leased_agent_id is None:
                await unit_of_work.commit()
                return False

            candidate_agent_id = candidate_task.leased_agent_id
            agent = await unit_of_work.agents.get_for_update(candidate_agent_id)
            job = await unit_of_work.collection_jobs.get_for_update(candidate_job.id)
            task = await unit_of_work.collection_tasks.get_for_update(candidate_task.id)
            if (
                agent is None
                or job is None
                or task is None
                or task.collection_job_id != job.id
                or task.leased_agent_id != candidate_agent_id
                or task.fencing_token != candidate_task.fencing_token
                or task.lease_expires_at != candidate_task.lease_expires_at
                or task.lease_expires_at is None
                or task.lease_expires_at > recovered_at
                or task.status not in ACTIVE_LEASE_STATES
            ):
                await unit_of_work.commit()
                return False

            profile = (
                await unit_of_work.browser_profiles.get(task.leased_profile_id)
                if task.leased_profile_id is not None
                else None
            )
            attempt = await unit_of_work.task_attempts.get_by_task_fencing(
                task.id, task.fencing_token
            )
            if attempt is None or attempt.outcome is not None:
                raise TaskRuntimeInvariantViolation(
                    "expired lease does not have one active task attempt"
                )

            if task.desired_state is DesiredState.PAUSED:
                task.status = CollectionState.PAUSED
                task.failure_code = None
            elif task.desired_state is DesiredState.CANCELLED:
                task.status = CollectionState.CANCELLED
                task.failure_code = None
            else:
                task.status = CollectionState.QUEUED
                task.failure_code = "lease_expired"
            task.leased_agent_id = None
            task.leased_profile_id = None
            task.lease_token_hash = None
            task.lease_expires_at = None
            task.fencing_token += 1
            await unit_of_work.collection_tasks.update_runtime_state(task, recovered_at)
            await unit_of_work.agents.clear_current_task(agent.id, task.id, recovered_at)
            if profile is not None and profile.status == "busy":
                profile.status = "ready"
                profile.updated_at = recovered_at
                await unit_of_work.browser_profiles.upsert(profile)
            await unit_of_work.task_attempts.update_outcome(
                replace(
                    attempt,
                    released_at=recovered_at,
                    outcome=TaskAttemptOutcome.LEASE_EXPIRED,
                    failure_code="lease_expired",
                )
            )
            tasks = await unit_of_work.collection_tasks.list_for_job_for_update(job.id)
            await _aggregate_job(unit_of_work, job, tasks, recovered_at)
            await unit_of_work.commit()
            return True


async def _aggregate_job(
    unit_of_work: UnitOfWork,
    job: CollectionJob,
    tasks: list[CollectionTask],
    aggregated_at: datetime,
) -> None:
    previous = (job.status, job.completed_at)
    has_active = any(task.status in ACTIVE_LEASE_STATES for task in tasks)

    if job.desired_state is DesiredState.PAUSED:
        job.status = CollectionState.PAUSING if has_active else CollectionState.PAUSED
        job.completed_at = None
    elif job.desired_state is DesiredState.CANCELLED:
        if has_active:
            job.status = CollectionState.CANCELLING
            job.completed_at = None
        else:
            job.status = CollectionState.CANCELLED
            job.completed_at = aggregated_at
    elif has_active:
        job.status = CollectionState.RUNNING
        job.completed_at = None
    elif any(task.status is CollectionState.QUEUED for task in tasks):
        job.status = CollectionState.QUEUED
        job.completed_at = None
    elif tasks and all(task.status is CollectionState.SUCCEEDED for task in tasks):
        job.status = CollectionState.SUCCEEDED
        job.completed_at = aggregated_at
    elif tasks and all(task.status is CollectionState.FAILED for task in tasks):
        job.status = CollectionState.FAILED
        job.completed_at = aggregated_at
    else:
        job.status = CollectionState.PARTIALLY_SUCCEEDED
        job.completed_at = aggregated_at

    if previous != (job.status, job.completed_at):
        job.version += 1
        job.updated_at = aggregated_at
        await unit_of_work.collection_jobs.update_runtime_state(job, aggregated_at)
        if job.status in TERMINAL_COLLECTION_STATES:
            await add_event_with_outbox(
                unit_of_work,
                terminal_event(job, tasks, aggregated_at),
            )

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from crawler_api.domain.entities import (
    BrowserProfile,
    CollectionAgent,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.enums import CollectionState
from crawler_api.domain.task_attempts import CollectionTaskAttempt
from crawler_contracts.agent_protocol import ClaimTaskRequest, TaskLease
from pydantic import SecretStr

from .agent_authentication import AgentSessionAuthenticator
from .ports import LeaseTokenManager, UnitOfWork


class AgentTaskClaimUnavailable(Exception):
    pass


class ClaimRequestConflict(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ClaimCandidate:
    job: CollectionJob
    task: CollectionTask
    profile: BrowserProfile


class AgentTaskClaimer:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: AgentSessionAuthenticator,
        lease_tokens: LeaseTokenManager,
        lease_seconds: int,
        agent_offline_seconds: int,
        recover_expired_lease: Callable[[], Awaitable[bool]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._lease_tokens = lease_tokens
        self._lease_seconds = lease_seconds
        self._agent_offline_seconds = agent_offline_seconds
        self._recover_expired_lease = recover_expired_lease
        self._clock = clock

    async def claim(
        self,
        *,
        session_token: SecretStr,
        request: ClaimTaskRequest,
    ) -> TaskLease | None:
        if self._recover_expired_lease is not None:
            await self._recover_expired_lease()
        claimed_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            authenticated = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                session_token=session_token,
                expected_agent_id=request.agent_id,
                authenticated_at=claimed_at,
            )
            agent = authenticated.agent

            existing_attempt = await unit_of_work.task_attempts.get(request.claim_request_id)
            if existing_attempt is not None:
                lease = await self._replay(
                    unit_of_work=unit_of_work,
                    agent=agent,
                    attempt=existing_attempt,
                    claimed_at=claimed_at,
                )
                await unit_of_work.commit()
                return lease

            ready_profiles = await self._ready_profiles(unit_of_work, agent, claimed_at)
            if agent.current_task_id is not None or not ready_profiles:
                raise AgentTaskClaimUnavailable

            sources = sorted(
                {profile.source for profile in ready_profiles}, key=lambda source: source.value
            )
            job = await unit_of_work.collection_jobs.lock_next_claimable(sources)
            if job is None:
                await unit_of_work.commit()
                return None
            task = await unit_of_work.collection_tasks.lock_next_claimable(job.id, sources)
            if task is None:
                await unit_of_work.commit()
                return None

            profile = next(profile for profile in ready_profiles if profile.source is task.source)
            lease = await self._assign(
                unit_of_work=unit_of_work,
                agent=agent,
                candidate=ClaimCandidate(job=job, task=task, profile=profile),
                claim_request_id=request.claim_request_id,
                claimed_at=claimed_at,
            )
            await unit_of_work.commit()
            return lease

    async def _ready_profiles(
        self,
        unit_of_work: UnitOfWork,
        agent: CollectionAgent,
        claimed_at: datetime,
    ) -> list[BrowserProfile]:
        stale_before = claimed_at - timedelta(seconds=self._agent_offline_seconds)
        if (
            agent.status != "online"
            or agent.heartbeat_at is None
            or agent.heartbeat_at < stale_before
            or not agent.capabilities.supported_sources
        ):
            raise AgentTaskClaimUnavailable

        supported_sources = set(agent.capabilities.supported_sources)
        profiles = await unit_of_work.browser_profiles.list_for_agent(agent.id)
        return [
            profile
            for profile in profiles
            if profile.source in supported_sources
            and profile.status == "ready"
            and profile.updated_at is not None
            and profile.updated_at >= stale_before
        ]

    async def _assign(
        self,
        *,
        unit_of_work: UnitOfWork,
        agent: CollectionAgent,
        candidate: ClaimCandidate,
        claim_request_id: UUID,
        claimed_at: datetime,
    ) -> TaskLease:
        job, task, profile = candidate.job, candidate.task, candidate.profile
        fencing_token = task.fencing_token + 1
        lease_expires_at = claimed_at + timedelta(seconds=self._lease_seconds)
        lease_token = self._lease_tokens.issue(
            attempt_id=claim_request_id,
            agent_id=agent.id,
            task_id=task.id,
            fencing_token=fencing_token,
        )

        task.status = CollectionState.RUNNING
        task.leased_agent_id = agent.id
        task.leased_profile_id = profile.id
        task.lease_token_hash = self._lease_tokens.hash(lease_token)
        task.fencing_token = fencing_token
        task.lease_expires_at = lease_expires_at
        task.attempt_count += 1
        task.failure_code = None
        await unit_of_work.collection_tasks.update_lease_state(task, claimed_at)

        profile.status = "busy"
        profile.updated_at = claimed_at
        await unit_of_work.browser_profiles.upsert(profile)

        agent.current_task_id = task.id
        await unit_of_work.agents.update(agent)

        if job.status is not CollectionState.RUNNING:
            job.status = CollectionState.RUNNING
            job.version += 1
            job.updated_at = claimed_at
            await unit_of_work.collection_jobs.update_runtime_state(job, claimed_at)

        await unit_of_work.task_attempts.add(
            CollectionTaskAttempt(
                id=claim_request_id,
                collection_task_id=task.id,
                agent_id=agent.id,
                fencing_token=fencing_token,
                claimed_at=claimed_at,
            )
        )
        return self._lease(
            agent=agent,
            task=task,
            profile=profile,
            attempt_id=claim_request_id,
            lease_token=lease_token,
        )

    async def _replay(
        self,
        *,
        unit_of_work: UnitOfWork,
        agent: CollectionAgent,
        attempt: CollectionTaskAttempt,
        claimed_at: datetime,
    ) -> TaskLease:
        if (
            attempt.agent_id != agent.id
            or attempt.released_at is not None
            or attempt.outcome is not None
        ):
            raise ClaimRequestConflict

        visible_task = await unit_of_work.collection_tasks.get(attempt.collection_task_id)
        if visible_task is None:
            raise ClaimRequestConflict
        job = await unit_of_work.collection_jobs.get_for_update(visible_task.collection_job_id)
        task = await unit_of_work.collection_tasks.get_for_update(visible_task.id)
        if job is None or task is None:
            raise ClaimRequestConflict
        profile = (
            await unit_of_work.browser_profiles.get(task.leased_profile_id)
            if task.leased_profile_id is not None
            else None
        )
        if (
            profile is None
            or profile.agent_id != agent.id
            or profile.source is not task.source
            or agent.current_task_id != task.id
            or task.leased_agent_id != agent.id
            or task.fencing_token != attempt.fencing_token
            or task.lease_expires_at is None
            or task.lease_expires_at <= claimed_at
            or task.lease_token_hash is None
        ):
            raise ClaimRequestConflict

        lease_token = self._lease_tokens.issue(
            attempt_id=attempt.id,
            agent_id=agent.id,
            task_id=task.id,
            fencing_token=task.fencing_token,
        )
        if not self._lease_tokens.matches(lease_token, task.lease_token_hash):
            raise ClaimRequestConflict
        return self._lease(
            agent=agent,
            task=task,
            profile=profile,
            attempt_id=attempt.id,
            lease_token=lease_token,
        )

    @staticmethod
    def _lease(
        *,
        agent: CollectionAgent,
        task: CollectionTask,
        profile: BrowserProfile,
        attempt_id: UUID,
        lease_token: str,
    ) -> TaskLease:
        if task.lease_expires_at is None:
            raise RuntimeError("claimed task is missing lease expiry")
        return TaskLease(
            agent_id=agent.id,
            task_id=task.id,
            attempt_id=attempt_id,
            source=task.source,
            profile_key=profile.profile_key,
            keyword=task.keyword,
            region=task.region,
            page_limit=task.page_limit,
            page_checkpoint=task.page_checkpoint,
            lease_token=lease_token,
            fencing_token=task.fencing_token,
            lease_expires_at=task.lease_expires_at,
        )

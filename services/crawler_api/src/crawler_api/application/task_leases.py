from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from crawler_api.domain.entities import (
    BrowserProfile,
    CollectionAgent,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.enums import CollectionState
from crawler_api.domain.task_attempts import CollectionTaskAttempt
from crawler_contracts.agent_protocol import LeaseReference
from pydantic import SecretStr

from .agent_authentication import AgentSessionAuthenticator
from .ports import LeaseTokenManager, UnitOfWork


class TaskLeaseStateConflict(Exception):
    """Uniform failure for a missing, stale, expired, or mismatched task lease."""


ACTIVE_LEASE_STATES = frozenset(
    {
        CollectionState.RUNNING,
        CollectionState.AWAITING_HUMAN,
        CollectionState.PAUSING,
        CollectionState.CANCELLING,
    }
)


@dataclass(frozen=True, slots=True)
class AuthenticatedTaskLease:
    agent: CollectionAgent
    job: CollectionJob
    task: CollectionTask
    profile: BrowserProfile
    attempt: CollectionTaskAttempt


class ActiveTaskLeaseAuthenticator:
    def __init__(
        self,
        *,
        authenticator: AgentSessionAuthenticator,
        lease_tokens: LeaseTokenManager,
    ) -> None:
        self._authenticator = authenticator
        self._lease_tokens = lease_tokens

    async def authenticate(
        self,
        *,
        unit_of_work: UnitOfWork,
        session_token: SecretStr,
        expected_task_id: UUID,
        lease_reference: LeaseReference,
        authenticated_at: datetime,
    ) -> AuthenticatedTaskLease:
        if lease_reference.task_id != expected_task_id:
            raise TaskLeaseStateConflict

        authenticated = await self._authenticator.authenticate(
            unit_of_work=unit_of_work,
            session_token=session_token,
            expected_agent_id=lease_reference.agent_id,
            authenticated_at=authenticated_at,
        )
        agent = authenticated.agent

        # The Agent is already locked. Discover the owning Job without a lock,
        # then preserve the global Agent -> Job -> Task lock order.
        visible_task = await unit_of_work.collection_tasks.get(expected_task_id)
        if visible_task is None:
            raise TaskLeaseStateConflict
        job = await unit_of_work.collection_jobs.get_for_update(visible_task.collection_job_id)
        task = await unit_of_work.collection_tasks.get_for_update(expected_task_id)
        if job is None or task is None or task.collection_job_id != job.id:
            raise TaskLeaseStateConflict

        profile = (
            await unit_of_work.browser_profiles.get(task.leased_profile_id)
            if task.leased_profile_id is not None
            else None
        )
        attempt = await unit_of_work.task_attempts.get_by_task_fencing(task.id, task.fencing_token)
        if (
            agent.current_task_id != task.id
            or task.status not in ACTIVE_LEASE_STATES
            or task.leased_agent_id != agent.id
            or task.lease_token_hash is None
            or task.lease_expires_at is None
            or task.lease_expires_at <= authenticated_at
            or task.lease_expires_at != lease_reference.lease_expires_at
            or task.fencing_token != lease_reference.fencing_token
            or profile is None
            or profile.agent_id != agent.id
            or profile.source is not task.source
            or attempt is None
            or attempt.agent_id != agent.id
            or attempt.released_at is not None
            or attempt.outcome is not None
            or not self._lease_tokens.matches(
                lease_reference.lease_token.get_secret_value(),
                task.lease_token_hash,
            )
        ):
            raise TaskLeaseStateConflict

        return AuthenticatedTaskLease(
            agent=agent,
            job=job,
            task=task,
            profile=profile,
            attempt=attempt,
        )

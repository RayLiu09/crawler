from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from crawler_api.application.ports import UnitOfWork

from .repositories import (
    SqlAlchemyAgentSessionRepository,
    SqlAlchemyApiAccessTokenRepository,
    SqlAlchemyApiClientRepository,
    SqlAlchemyBrowserProfileRepository,
    SqlAlchemyCollectionAgentRepository,
    SqlAlchemyCollectionEventRepository,
    SqlAlchemyCollectionJobCallbackConfigRepository,
    SqlAlchemyCollectionJobCallbackCredentialRotationRepository,
    SqlAlchemyCollectionJobRepository,
    SqlAlchemyCollectionTaskAttemptRepository,
    SqlAlchemyCollectionTaskRepository,
    SqlAlchemyParsedJobRecordRepository,
    SqlAlchemyParsedRecordBatchRepository,
    SqlAlchemyWebhookDeliveryReplayRepository,
    SqlAlchemyWebhookOutboxRepository,
)


class SqlAlchemyUnitOfWork:
    """Own one explicit database transaction for an application use case."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("unit of work is already active")
        self._session = self._session_factory()
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        try:
            if exc_type is not None or not self._committed:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
            self._committed = False

    @property
    def api_clients(self) -> SqlAlchemyApiClientRepository:
        return SqlAlchemyApiClientRepository(self._require_session())

    @property
    def api_access_tokens(self) -> SqlAlchemyApiAccessTokenRepository:
        return SqlAlchemyApiAccessTokenRepository(self._require_session())

    @property
    def agents(self) -> SqlAlchemyCollectionAgentRepository:
        return SqlAlchemyCollectionAgentRepository(self._require_session())

    @property
    def agent_sessions(self) -> SqlAlchemyAgentSessionRepository:
        return SqlAlchemyAgentSessionRepository(self._require_session())

    @property
    def browser_profiles(self) -> SqlAlchemyBrowserProfileRepository:
        return SqlAlchemyBrowserProfileRepository(self._require_session())

    @property
    def collection_jobs(self) -> SqlAlchemyCollectionJobRepository:
        return SqlAlchemyCollectionJobRepository(self._require_session())

    @property
    def collection_tasks(self) -> SqlAlchemyCollectionTaskRepository:
        return SqlAlchemyCollectionTaskRepository(self._require_session())

    @property
    def task_attempts(self) -> SqlAlchemyCollectionTaskAttemptRepository:
        return SqlAlchemyCollectionTaskAttemptRepository(self._require_session())

    @property
    def parsed_job_records(self) -> SqlAlchemyParsedJobRecordRepository:
        return SqlAlchemyParsedJobRecordRepository(self._require_session())

    @property
    def parsed_record_batches(self) -> SqlAlchemyParsedRecordBatchRepository:
        return SqlAlchemyParsedRecordBatchRepository(self._require_session())

    @property
    def collection_events(self) -> SqlAlchemyCollectionEventRepository:
        return SqlAlchemyCollectionEventRepository(self._require_session())

    @property
    def callback_configs(self) -> SqlAlchemyCollectionJobCallbackConfigRepository:
        return SqlAlchemyCollectionJobCallbackConfigRepository(self._require_session())

    @property
    def callback_credential_rotations(
        self,
    ) -> SqlAlchemyCollectionJobCallbackCredentialRotationRepository:
        return SqlAlchemyCollectionJobCallbackCredentialRotationRepository(self._require_session())

    @property
    def webhook_outbox(self) -> SqlAlchemyWebhookOutboxRepository:
        return SqlAlchemyWebhookOutboxRepository(self._require_session())

    @property
    def webhook_delivery_replays(self) -> SqlAlchemyWebhookDeliveryReplayRepository:
        return SqlAlchemyWebhookDeliveryReplayRepository(self._require_session())

    async def commit(self) -> None:
        await self._require_session().commit()
        self._committed = True

    async def rollback(self) -> None:
        await self._require_session().rollback()
        self._committed = False

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work must be entered before use")
        return self._session


def create_unit_of_work(
    session_factory: async_sessionmaker[AsyncSession],
) -> UnitOfWork:
    """Build the application port while statically checking adapter compatibility."""
    return SqlAlchemyUnitOfWork(session_factory)

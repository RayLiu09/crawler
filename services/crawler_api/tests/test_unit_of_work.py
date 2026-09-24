from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def make_unit_of_work() -> tuple[SqlAlchemyUnitOfWork, Any]:
    session = Mock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session_factory = Mock(return_value=session)
    unit_of_work = SqlAlchemyUnitOfWork(cast(async_sessionmaker[AsyncSession], session_factory))
    return unit_of_work, session


async def test_uncommitted_unit_of_work_rolls_back_on_exit() -> None:
    unit_of_work, session = make_unit_of_work()

    async with unit_of_work:
        assert unit_of_work.api_clients is not None
        assert unit_of_work.agent_sessions is not None
        assert unit_of_work.task_attempts is not None
        assert unit_of_work.collection_events is not None

    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


async def test_committed_unit_of_work_does_not_rollback_on_clean_exit() -> None:
    unit_of_work, session = make_unit_of_work()

    async with unit_of_work:
        await unit_of_work.commit()

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()


async def test_exception_rolls_back_even_after_repositories_are_created() -> None:
    unit_of_work, session = make_unit_of_work()

    with pytest.raises(RuntimeError, match="failed use case"):
        async with unit_of_work:
            assert unit_of_work.collection_jobs is not None
            raise RuntimeError("failed use case")

    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


async def test_unit_of_work_cannot_be_used_outside_context() -> None:
    unit_of_work, _ = make_unit_of_work()

    with pytest.raises(RuntimeError, match="must be entered"):
        _ = unit_of_work.collection_tasks

import os

import pytest
from crawler_api.config import get_settings
from crawler_api.domain.entities import (
    ApiClient,
    BrowserProfile,
    CollectionAgent,
    CollectionJob,
    CollectionTask,
)
from crawler_api.infrastructure.database.models import ApiClientRow, CollectionAgentRow
from crawler_api.infrastructure.database.repositories import (
    SqlAlchemyApiClientRepository,
    SqlAlchemyBrowserProfileRepository,
    SqlAlchemyCollectionAgentRepository,
    SqlAlchemyCollectionJobRepository,
    SqlAlchemyCollectionTaskRepository,
)
from crawler_api.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import ALL_API_SCOPES, SourceCode

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)


async def test_repository_roundtrip_is_rolled_back() -> None:
    engine = create_database_engine(get_settings())
    session_factory = create_session_factory(engine)
    client = ApiClient(
        tenant_id="integration-test-tenant",
        display_name="Integration test tenant",
        allowed_scopes=list(ALL_API_SCOPES),
        config_fingerprint="integration-config-fingerprint",
    )
    agent = CollectionAgent(
        device_credential_hash="integration-device-credential-hash",
        display_name="Integration test agent",
    )
    job = CollectionJob(
        client_id=client.id,
        external_request_id="integration-request-1",
        idempotency_key_hash="integration-idempotency-hash",
        request_payload=CollectionJobPayload(
            source="zhaopin",
            queries=[{"keyword": "Python", "region": "Beijing"}],
        ),
    )
    task = CollectionTask(
        collection_job_id=job.id,
        source=SourceCode.ZHAOPIN,
        keyword="Python",
        region="Beijing",
        page_limit=1,
    )
    profile = BrowserProfile(
        agent_id=agent.id,
        source=SourceCode.ZHAOPIN,
        profile_key="integration-primary",
        status="ready",
    )

    try:
        async with session_factory() as session:
            transaction = await session.begin()
            client_repository = SqlAlchemyApiClientRepository(session)
            agent_repository = SqlAlchemyCollectionAgentRepository(session)
            job_repository = SqlAlchemyCollectionJobRepository(session)
            task_repository = SqlAlchemyCollectionTaskRepository(session)
            profile_repository = SqlAlchemyBrowserProfileRepository(session)

            await client_repository.add(client)
            await agent_repository.add(agent)
            await job_repository.add(job)
            await task_repository.add(task)
            await session.flush()
            await profile_repository.upsert(profile)

            stored_client = await client_repository.get_by_tenant_id(client.tenant_id)
            stored_agent = await agent_repository.get(agent.id)
            stored_job = await job_repository.get_for_client(job.id, client.id)
            stored_task = await task_repository.get(task.id)
            stored_profiles = await profile_repository.list_for_agent(agent.id)

            assert stored_client is not None and stored_client.id == client.id
            assert stored_client.allowed_scopes == client.allowed_scopes
            assert stored_agent is not None and stored_agent.id == agent.id
            assert stored_agent.capabilities == agent.capabilities
            assert stored_job is not None and stored_job.id == job.id
            assert stored_job.request_payload == job.request_payload
            assert stored_task is not None and stored_task.id == task.id
            assert stored_task.source == task.source
            assert len(stored_profiles) == 1
            assert stored_profiles[0].profile_key == profile.profile_key

            await transaction.rollback()

        async with session_factory() as verification_session:
            assert await verification_session.get(ApiClientRow, client.id) is None
            assert await verification_session.get(CollectionAgentRow, agent.id) is None
    finally:
        await engine.dispose()

import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_events import (
    CollectionEventJobNotFound,
    CollectionEventReader,
)
from crawler_api.application.collection_jobs import (
    CollectionJobControlAction,
    CollectionJobController,
    CollectionJobSubmitter,
)
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.models import (
    CollectionEventRow,
    CollectionJobCallbackConfigRow,
    WebhookOutboxRow,
)
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from crawler_contracts.events import CollectionEventType
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database",
)

TENANT_ID = "collection-events-integration-tenant"
TENANT_KEY = "collection-events-integration-key"
OTHER_TENANT_ID = "collection-events-other-tenant"
OTHER_TENANT_KEY = "collection-events-other-key"
ACCESS_TOKEN = "cat_collection_events_integration_token"
OTHER_ACCESS_TOKEN = "cat_collection_events_other_token"
CALLBACK_URL = "http://upstream.internal/api/job-collection/events"
CALLBACK_TOKEN = "callback_events_token_123456789012345678901234"
BASE_TIME = datetime(2026, 9, 17, 17, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def tenant_entry(
    tenant_id: str,
    tenant_key: str,
    *,
    callback_allowlist: list[str] | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "tenantId": tenant_id,
        "displayName": tenant_id,
        "enabled": True,
        "tenantKeyHashes": [
            f"sha256:{hashlib.sha256(tenant_key.encode()).hexdigest()}"
        ],
    }
    if callback_allowlist is not None:
        entry["callbackAllowlist"] = callback_allowlist
    return entry


def build_allowlist() -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                tenant_entry(
                    TENANT_ID,
                    TENANT_KEY,
                    callback_allowlist=[CALLBACK_URL],
                ),
                tenant_entry(OTHER_TENANT_ID, OTHER_TENANT_KEY),
            ],
        }
    )


async def test_collection_events_are_atomic_idempotent_paginated_and_rolled_back() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    job_id: UUID | None = None
    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()
            session_factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )

            def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
                return SqlAlchemyUnitOfWork(session_factory)

            allowlist = build_allowlist()
            await TenantAllowlistSynchronizer(unit_of_work_factory).synchronize(
                allowlist
            )
            issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=7200,
                clock=lambda: BASE_TIME,
                token_generator=lambda: ACCESS_TOKEN,
            ).issue(SecretStr(TENANT_KEY))
            other_issued = await AccessTokenIssuer(
                allowlist=allowlist,
                unit_of_work_factory=unit_of_work_factory,
                hash_token=token_hasher.hash,
                ttl_seconds=7200,
                clock=lambda: BASE_TIME,
                token_generator=lambda: OTHER_ACCESS_TOKEN,
            ).issue(SecretStr(OTHER_TENANT_KEY))
            authenticator = CallerAccessTokenAuthenticator(token_hasher.hash)
            clock = MutableClock(BASE_TIME + timedelta(minutes=1))
            submitter = CollectionJobSubmitter(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                tenant_allowlist=allowlist,
                callback_token_protector=CallbackTokenProtector(
                    encryption_key=settings.callback_token_encryption_key,
                    fingerprint_key=settings.callback_token_fingerprint_key,
                    encryption_key_version=settings.callback_token_encryption_key_version,
                ),
                clock=clock,
            )
            request = SubmitCollectionJobRequest.model_validate(
                {
                    "externalRequestId": "jobreq_events_integration_001",
                    "source": "zhaopin",
                    "queries": [
                        {"keyword": "Python", "region": "Shanghai"},
                        {"keyword": "Java", "region": "Beijing"},
                    ],
                    "callback": {
                        "url": CALLBACK_URL,
                        "accessToken": CALLBACK_TOKEN,
                        "tokenType": "Bearer",
                    },
                }
            )
            first = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("events-integration-create"),
                request=request,
            )
            replay = await submitter.submit(
                access_token=SecretStr(issued.access_token),
                idempotency_key=SecretStr("events-integration-create"),
                request=request,
            )
            job_id = first.job.id
            assert not replay.created

            controller = CollectionJobController(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                clock=clock,
            )
            clock.now += timedelta(minutes=1)
            await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.PAUSE,
            )
            await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.PAUSE,
            )
            clock.now += timedelta(minutes=1)
            await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.RESUME,
            )
            clock.now += timedelta(minutes=1)
            await controller.control(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                action=CollectionJobControlAction.CANCEL,
            )

            reader = CollectionEventReader(
                unit_of_work_factory=unit_of_work_factory,
                authenticator=authenticator,
                clock=clock,
            )
            first_page = await reader.list_for_job(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                cursor=None,
                limit=2,
            )
            second_page = await reader.list_for_job(
                access_token=SecretStr(issued.access_token),
                job_id=job_id,
                cursor=first_page.next_cursor,
                limit=3,
            )
            events = first_page.items + second_page.items

            assert [event.event_type for event in events[:3]] == [
                CollectionEventType.JOB_ACCEPTED,
                CollectionEventType.JOB_DESIRED_STATE_CHANGED,
                CollectionEventType.JOB_DESIRED_STATE_CHANGED,
            ]
            assert {event.event_type for event in events[3:]} == {
                CollectionEventType.JOB_DESIRED_STATE_CHANGED,
                CollectionEventType.JOB_TERMINAL,
            }
            assert [event.job_version for event in events[:3]] == [1, 2, 3]
            assert {event.job_version for event in events[3:]} == {4}
            assert first_page.next_cursor is not None
            assert second_page.next_cursor is None
            assert all(event.collection_job_id == job_id for event in events)
            assert all(
                event.external_request_id == request.external_request_id
                for event in events
            )

            with pytest.raises(CollectionEventJobNotFound):
                await reader.list_for_job(
                    access_token=SecretStr(other_issued.access_token),
                    job_id=job_id,
                    cursor=None,
                    limit=10,
                )

            payloads = (
                await connection.scalars(
                    select(CollectionEventRow.payload)
                    .where(CollectionEventRow.collection_job_id == job_id)
                    .order_by(CollectionEventRow.occurred_at, CollectionEventRow.id)
                )
            ).all()
            assert len(payloads) == 5
            forbidden = {
                "tenantId",
                "clientId",
                "agentId",
                "profileKey",
                "leaseToken",
                "accessToken",
            }
            assert all(not (forbidden & set(payload)) for payload in payloads)
            assert (
                await connection.scalar(
                    select(func.count()).select_from(CollectionEventRow)
                )
                == 5
            )
            callback_config_id = await connection.scalar(
                select(CollectionJobCallbackConfigRow.id).where(
                    CollectionJobCallbackConfigRow.collection_job_id == job_id
                )
            )
            assert callback_config_id is not None
            outbox_rows = (
                await connection.execute(
                    select(
                        WebhookOutboxRow.event_id,
                        WebhookOutboxRow.callback_config_id,
                        WebhookOutboxRow.attempt_count,
                        WebhookOutboxRow.next_attempt_at,
                    ).order_by(WebhookOutboxRow.next_attempt_at, WebhookOutboxRow.event_id)
                )
            ).all()
            assert len(outbox_rows) == 5
            assert {row.event_id for row in outbox_rows} == {
                event.event_id for event in events
            }
            assert all(
                row.callback_config_id == callback_config_id
                and row.attempt_count == 0
                for row in outbox_rows
            )
            assert {row.next_attempt_at for row in outbox_rows} == {
                event.occurred_at for event in events
            }

            await outer_transaction.rollback()

        async with engine.connect() as verification_connection:
            assert job_id is not None
            assert (
                await verification_connection.scalar(
                    select(func.count())
                    .select_from(CollectionEventRow)
                    .where(CollectionEventRow.collection_job_id == job_id)
                )
                == 0
            )
    finally:
        await engine.dispose()

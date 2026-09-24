from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from crawler_api.api.v1.collection_jobs import router
from crawler_api.application.webhook_operations import (
    WebhookDeliveryCursorInvalid,
    WebhookDeliveryNotFound,
    WebhookDeliveryOperator,
    WebhookDeliveryReplayConflict,
    decode_webhook_delivery_cursor,
)
from crawler_api.domain.entities import CollectionJob
from crawler_api.domain.webhooks import (
    StoredWebhookDelivery,
    WebhookDeliveryReplay,
    WebhookOutboxStatus,
)
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import SourceCode
from crawler_contracts.webhooks import ReplayWebhookDeliveryResponse
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
CLIENT_ID = UUID("11111111-aaaa-4111-8111-111111111111")
OTHER_CLIENT_ID = UUID("22222222-bbbb-4222-8222-222222222222")


def build_job(*, client_id: UUID = CLIENT_ID) -> CollectionJob:
    return CollectionJob(
        client_id=client_id,
        external_request_id="jobreq_webhook_operations_001",
        idempotency_key_hash="sha256:" + "a" * 64,
        request_payload=CollectionJobPayload(
            source=SourceCode.ZHAOPIN,
            queries=[{"keyword": "Python", "region": "Shanghai"}],
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def build_delivery(
    *,
    created_at: datetime = NOW,
    failure_code: str = "retry_exhausted_http_503",
    token_expires_at: datetime = NOW + timedelta(days=1),
    credential_available: bool = True,
) -> StoredWebhookDelivery:
    return StoredWebhookDelivery(
        id=uuid4(),
        event_id=uuid4(),
        event_type="collection.job.accepted",
        status=WebhookOutboxStatus.PERMANENTLY_FAILED,
        attempt_count=8,
        replay_count=0,
        next_attempt_at=NOW - timedelta(minutes=1),
        delivered_at=None,
        last_failure_code=failure_code,
        dispatch_claim_id=None,
        dispatch_lease_expires_at=None,
        last_replayed_at=None,
        created_at=created_at,
        credential_available=credential_available,
        token_expires_at=token_expires_at,
    )


class JobRepository:
    def __init__(self, job: CollectionJob) -> None:
        self.job = job

    async def get_for_client(self, job_id: UUID, client_id: UUID) -> CollectionJob | None:
        if self.job.id == job_id and self.job.client_id == client_id:
            return self.job
        return None


class OutboxRepository:
    def __init__(self, job: CollectionJob, deliveries: list[StoredWebhookDelivery]) -> None:
        self.job = job
        self.deliveries = deliveries

    async def list_for_job(self, job_id: UUID, **kwargs: object) -> list[StoredWebhookDelivery]:
        status = kwargs["status"]
        after_created_at = kwargs["after_created_at"]
        after_id = kwargs["after_id"]
        limit = kwargs["limit"]
        assert isinstance(status, WebhookOutboxStatus)
        assert isinstance(limit, int)
        if job_id != self.job.id:
            return []
        values = sorted(self.deliveries, key=lambda row: (row.created_at, row.id))
        values = [row for row in values if row.status is status]
        if isinstance(after_created_at, datetime) and isinstance(after_id, UUID):
            values = [
                row for row in values if (row.created_at, row.id) > (after_created_at, after_id)
            ]
        return values[:limit]

    async def get_for_client_for_update(self, **kwargs: object) -> StoredWebhookDelivery | None:
        if kwargs["job_id"] != self.job.id or kwargs["client_id"] != self.job.client_id:
            return None
        return next(
            (row for row in self.deliveries if row.id == kwargs["outbox_id"]),
            None,
        )

    async def activate_replay(
        self,
        outbox_id: UUID,
        *,
        expected_replay_count: int,
        replayed_at: datetime,
    ) -> bool:
        for index, row in enumerate(self.deliveries):
            if row.id != outbox_id or row.replay_count != expected_replay_count:
                continue
            self.deliveries[index] = replace(
                row,
                status=WebhookOutboxStatus.PENDING,
                attempt_count=0,
                replay_count=row.replay_count + 1,
                last_replayed_at=replayed_at,
                next_attempt_at=replayed_at,
                last_failure_code=None,
            )
            return True
        return False


class ReplayRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[UUID, str], WebhookDeliveryReplay] = {}

    async def add(self, replay: WebhookDeliveryReplay) -> None:
        self.items[(replay.webhook_outbox_id, replay.idempotency_key_hash)] = replay

    async def get(
        self, webhook_outbox_id: UUID, idempotency_key_hash: str
    ) -> WebhookDeliveryReplay | None:
        return self.items.get((webhook_outbox_id, idempotency_key_hash))


class UnitOfWork:
    def __init__(self, job: CollectionJob, deliveries: list[StoredWebhookDelivery]) -> None:
        self.collection_jobs = JobRepository(job)
        self.webhook_outbox = OutboxRepository(job, deliveries)
        self.webhook_delivery_replays = ReplayRepository()
        self.commit_count = 0

    async def __aenter__(self) -> "UnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


class Authenticator:
    def __init__(self, client_id: UUID = CLIENT_ID) -> None:
        self.client_id = client_id

    async def authenticate(self, **_: object) -> object:
        return SimpleNamespace(client=SimpleNamespace(id=self.client_id))


def operator(
    unit_of_work: UnitOfWork, *, client_id: UUID = CLIENT_ID
) -> WebhookDeliveryOperator:
    return WebhookDeliveryOperator(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=Authenticator(client_id),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


async def test_query_is_tenant_scoped_paginated_and_reports_replay_eligibility() -> None:
    job = build_job()
    deliveries = [
        build_delivery(created_at=NOW),
        build_delivery(created_at=NOW + timedelta(seconds=1)),
        build_delivery(
            created_at=NOW + timedelta(seconds=2),
            token_expires_at=NOW,
        ),
    ]
    unit_of_work = UnitOfWork(job, deliveries)
    service = operator(unit_of_work)

    first = await service.list_for_job(
        access_token=SecretStr("caller-token"),
        job_id=job.id,
        status=WebhookOutboxStatus.PERMANENTLY_FAILED,
        cursor=None,
        limit=2,
    )
    second = await service.list_for_job(
        access_token=SecretStr("caller-token"),
        job_id=job.id,
        status=WebhookOutboxStatus.PERMANENTLY_FAILED,
        cursor=first.next_cursor,
        limit=2,
    )

    assert [item.outbox_id for item in first.items + second.items] == [
        row.id for row in deliveries
    ]
    assert [item.replay_eligible for item in first.items + second.items] == [True, True, False]
    assert first.next_cursor is not None
    assert second.next_cursor is None
    with pytest.raises(WebhookDeliveryCursorInvalid):
        decode_webhook_delivery_cursor(
            first.next_cursor,
            job.id,
            WebhookOutboxStatus.DELIVERED,
        )
    with pytest.raises(WebhookDeliveryNotFound):
        await operator(unit_of_work, client_id=OTHER_CLIENT_ID).list_for_job(
            access_token=SecretStr("other-token"),
            job_id=job.id,
            status=WebhookOutboxStatus.PERMANENTLY_FAILED,
            cursor=None,
            limit=2,
        )


async def test_controlled_replay_resets_budget_and_old_key_remains_deterministic() -> None:
    job = build_job()
    delivery = build_delivery()
    unit_of_work = UnitOfWork(job, [delivery])
    service = operator(unit_of_work)

    first = await service.replay(
        access_token=SecretStr("caller-token"),
        idempotency_key=SecretStr("replay-operation-001"),
        job_id=job.id,
        outbox_id=delivery.id,
    )
    replayed = unit_of_work.webhook_outbox.deliveries[0]
    second = await service.replay(
        access_token=SecretStr("caller-token"),
        idempotency_key=SecretStr("replay-operation-001"),
        job_id=job.id,
        outbox_id=delivery.id,
    )

    assert first == second
    assert first.replay_number == 1
    assert first.previous_attempt_count == 8
    assert first.previous_failure_code == "retry_exhausted_http_503"
    assert replayed.status is WebhookOutboxStatus.PENDING
    assert replayed.attempt_count == 0
    assert replayed.replay_count == 1
    assert replayed.next_attempt_at == NOW
    assert replayed.last_failure_code is None
    assert len(unit_of_work.webhook_delivery_replays.items) == 1


@pytest.mark.parametrize(
    "delivery",
    [
        build_delivery(failure_code="http_400"),
        build_delivery(token_expires_at=NOW),
        build_delivery(credential_available=False),
    ],
)
async def test_controlled_replay_rejects_ineligible_delivery(
    delivery: StoredWebhookDelivery,
) -> None:
    job = build_job()
    with pytest.raises(WebhookDeliveryReplayConflict):
        await operator(UnitOfWork(job, [delivery])).replay(
            access_token=SecretStr("caller-token"),
            idempotency_key=SecretStr("replay-operation-rejected"),
            job_id=job.id,
            outbox_id=delivery.id,
        )


class StubOperator:
    def __init__(self, result: object | Exception) -> None:
        self.result = result

    async def replay(self, **_: object) -> object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_replay_http_route_maps_success_and_conflict() -> None:
    job_id = uuid4()
    outbox_id = uuid4()
    result = ReplayWebhookDeliveryResponse(
        outboxId=outbox_id,
        replayNumber=1,
        replayedAt=NOW,
        previousAttemptCount=8,
        previousFailureCode="retry_exhausted_http_503",
    )
    app = FastAPI()
    app.include_router(router)
    app.state.webhook_delivery_operator = StubOperator(result)
    headers = {
        "Authorization": "Bearer caller-token",
        "Idempotency-Key": "replay-operation-001",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/collection-jobs/{job_id}/webhook-deliveries/{outbox_id}/replay",
            headers=headers,
        )
        app.state.webhook_delivery_operator = StubOperator(
            WebhookDeliveryReplayConflict()
        )
        conflict = await client.post(
            f"/v1/collection-jobs/{job_id}/webhook-deliveries/{outbox_id}/replay",
            headers=headers,
        )

    assert response.status_code == 202
    assert response.json()["previousAttemptCount"] == 8
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "webhook delivery is not eligible for replay"}

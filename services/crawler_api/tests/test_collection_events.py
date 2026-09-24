from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from crawler_api.api.v1.collection_jobs import router
from crawler_api.application.collection_events import (
    CollectionEventCursor,
    CollectionEventCursorInvalid,
    CollectionEventJobNotFound,
    CollectionEventReader,
    accepted_event,
    decode_event_cursor,
    desired_state_changed_event,
    encode_event_cursor,
    terminal_event,
)
from crawler_api.domain.entities import CollectionJob, CollectionTask
from crawler_api.domain.enums import CollectionState, DesiredState
from crawler_api.domain.events import StoredCollectionEvent
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import SourceCode
from crawler_contracts.events import CollectionEvent, CollectionEventType
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

NOW = datetime(2026, 9, 17, 17, 0, tzinfo=UTC)
CLIENT_ID = UUID("11111111-aaaa-4111-8111-111111111111")
OTHER_CLIENT_ID = UUID("22222222-bbbb-4222-8222-222222222222")


def build_job(*, client_id: UUID = CLIENT_ID) -> CollectionJob:
    return CollectionJob(
        client_id=client_id,
        external_request_id="jobreq_events_unit_001",
        idempotency_key_hash="sha256:" + "a" * 64,
        request_payload=CollectionJobPayload(
            source=SourceCode.ZHAOPIN,
            queries=[{"keyword": "Python", "region": "Shanghai"}],
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def build_task(job: CollectionJob) -> CollectionTask:
    return CollectionTask(
        collection_job_id=job.id,
        source=SourceCode.ZHAOPIN,
        keyword="Python",
        region="Shanghai",
        page_limit=10,
    )


class EventRepository:
    def __init__(self, events: list[StoredCollectionEvent]) -> None:
        self.events = events

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        after_occurred_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredCollectionEvent]:
        events = sorted(self.events, key=lambda event: (event.occurred_at, event.id))
        events = [event for event in events if event.collection_job_id == job_id]
        if after_occurred_at is not None and after_id is not None:
            events = [
                event
                for event in events
                if (event.occurred_at, event.id) > (after_occurred_at, after_id)
            ]
        return events[:limit]


class JobRepository:
    def __init__(self, job: CollectionJob) -> None:
        self.job = job

    async def get_for_client(
        self, job_id: UUID, client_id: UUID
    ) -> CollectionJob | None:
        if self.job.id == job_id and self.job.client_id == client_id:
            return self.job
        return None


class UnitOfWork:
    def __init__(self, job: CollectionJob, events: list[StoredCollectionEvent]) -> None:
        self.collection_jobs = JobRepository(job)
        self.collection_events = EventRepository(events)
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


def reader(
    unit_of_work: UnitOfWork, *, client_id: UUID = CLIENT_ID
) -> CollectionEventReader:
    return CollectionEventReader(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=Authenticator(client_id),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def event_sequence(job: CollectionJob) -> list[StoredCollectionEvent]:
    task = build_task(job)
    accepted = accepted_event(job, 1, NOW)
    job.desired_state = DesiredState.PAUSED
    job.status = CollectionState.PAUSED
    job.version = 2
    desired = desired_state_changed_event(job, "pause", NOW + timedelta(seconds=1))
    task.status = CollectionState.CANCELLED
    task.desired_state = DesiredState.CANCELLED
    task.processed_count = 3
    job.desired_state = DesiredState.CANCELLED
    job.status = CollectionState.CANCELLED
    job.version = 3
    job.completed_at = NOW
    terminal = terminal_event(job, [task], NOW + timedelta(seconds=2))
    return [accepted, desired, terminal]


def test_event_contract_rejects_mismatched_type_and_payload() -> None:
    job = build_job()
    event = accepted_event(job, 1, NOW)
    payload = {
        "schemaVersion": 1,
        "eventId": str(event.id),
        "eventType": CollectionEventType.JOB_TERMINAL,
        "occurredAt": NOW.isoformat(),
        "collectionJobId": str(job.id),
        "externalRequestId": job.external_request_id,
        "jobVersion": 1,
        "payload": event.payload.model_dump(mode="json", by_alias=True),
    }

    with pytest.raises(ValidationError, match="payload does not match eventType"):
        CollectionEvent.model_validate(payload)


async def test_reader_uses_job_bound_keyset_cursor_and_tenant_boundary() -> None:
    job = build_job()
    events = event_sequence(job)
    unit_of_work = UnitOfWork(job, events)
    service = reader(unit_of_work)

    first = await service.list_for_job(
        access_token=SecretStr("caller-token"),
        job_id=job.id,
        cursor=None,
        limit=2,
    )
    second = await service.list_for_job(
        access_token=SecretStr("caller-token"),
        job_id=job.id,
        cursor=first.next_cursor,
        limit=2,
    )

    assert [item.event_type for item in first.items + second.items] == [
        CollectionEventType.JOB_ACCEPTED,
        CollectionEventType.JOB_DESIRED_STATE_CHANGED,
        CollectionEventType.JOB_TERMINAL,
    ]
    assert first.next_cursor is not None
    assert second.next_cursor is None
    assert unit_of_work.commit_count == 2

    with pytest.raises(CollectionEventCursorInvalid):
        decode_event_cursor(first.next_cursor, uuid4())
    with pytest.raises(CollectionEventJobNotFound):
        await reader(unit_of_work, client_id=OTHER_CLIENT_ID).list_for_job(
            access_token=SecretStr("other-token"),
            job_id=job.id,
            cursor=None,
            limit=2,
        )


def test_event_cursor_rejects_malformed_and_naive_values() -> None:
    with pytest.raises(CollectionEventCursorInvalid):
        decode_event_cursor("not-a-cursor", uuid4())
    job_id = uuid4()
    naive = encode_event_cursor(
        CollectionEventCursor(
            job_id=job_id,
            occurred_at=NOW.replace(tzinfo=None),
            event_id=uuid4(),
        )
    )
    with pytest.raises(CollectionEventCursorInvalid):
        decode_event_cursor(naive, job_id)


class StubEventReader:
    def __init__(self, result: object | Exception) -> None:
        self.result = result

    async def list_for_job(self, **_: object) -> object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_events_http_route_returns_page_and_maps_invalid_cursor() -> None:
    job = build_job()
    event = accepted_event(job, 1, NOW)
    page = {
        "items": [
            CollectionEvent(
                eventId=event.id,
                eventType=event.event_type,
                occurredAt=event.occurred_at,
                collectionJobId=job.id,
                externalRequestId=job.external_request_id,
                jobVersion=event.job_version,
                payload=event.payload,
            ).model_dump(mode="json", by_alias=True)
        ],
        "nextCursor": None,
    }
    app = FastAPI()
    app.include_router(router)
    app.state.collection_event_reader = StubEventReader(page)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/collection-jobs/{job.id}/events",
            headers={"Authorization": "Bearer caller-token"},
        )
        app.state.collection_event_reader = StubEventReader(
            CollectionEventCursorInvalid()
        )
        invalid = await client.get(
            f"/v1/collection-jobs/{job.id}/events?cursor=bad",
            headers={"Authorization": "Bearer caller-token"},
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["eventType"] == "collection.job.accepted"
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid events cursor"}

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from crawler_api.api.v1.agent_tasks import router as agent_tasks_router
from crawler_api.api.v1.collection_jobs import router as collection_jobs_router
from crawler_api.application.caller_authentication import (
    CallerAccessTokenInsufficientScope,
)
from crawler_api.application.collection_jobs import CollectionJobNotFound
from crawler_api.application.parsed_records import (
    ParsedRecordBatchConflict,
    ParsedRecordBatchIngestor,
    ParsedRecordCursorInvalid,
    ParsedRecordReader,
    ParsedRecordTaskMismatch,
    RecordCursor,
    decode_record_cursor,
    encode_record_cursor,
)
from crawler_api.application.task_leases import AuthenticatedTaskLease
from crawler_api.domain.entities import (
    BrowserProfile,
    CollectionAgent,
    CollectionJob,
    CollectionTask,
)
from crawler_api.domain.enums import CollectionState
from crawler_api.domain.parsed_records import (
    ParsedRecordBatchReceipt,
    StoredParsedJobRecord,
)
from crawler_api.domain.task_attempts import CollectionTaskAttempt
from crawler_contracts.agent_protocol import ParsedRecordBatchRequest
from crawler_contracts.collection_jobs import CollectionJobPayload
from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import (
    ParsedJobFields,
    ParsedJobRecord,
    ParsedJobRecordPageResponse,
    calculate_payload_hash,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

NOW = datetime(2026, 9, 17, 16, 0, tzinfo=UTC)
AGENT_ID = UUID("11111111-aaaa-4111-8111-111111111111")
CLIENT_ID = UUID("22222222-bbbb-4222-8222-222222222222")
ATTEMPT_ID = UUID("33333333-cccc-4333-8333-333333333333")
BATCH_ID = UUID("44444444-dddd-4444-8444-444444444444")
LEASE_EXPIRY = NOW + timedelta(minutes=2)


def fields(title: str) -> ParsedJobFields:
    return ParsedJobFields(title=title, company_name="Example Co")


def parsed_record(
    title: str,
    *,
    source_url: str | None = None,
    keyword: str = "Python",
    region: str = "Shanghai",
    page: int = 1,
) -> ParsedJobRecord:
    parsed_fields = fields(title)
    return ParsedJobRecord(
        source=SourceCode.ZHAOPIN,
        source_job_id=f"source-{title}",
        source_url=source_url or f"https://jobs.example/{title}",
        keyword=keyword,
        region=region,
        page=page,
        fields=parsed_fields,
        collected_at=NOW,
        payload_hash=calculate_payload_hash(parsed_fields),
    )


def batch_request(
    records: list[ParsedJobRecord], *, batch_id: UUID = BATCH_ID
) -> ParsedRecordBatchRequest:
    return ParsedRecordBatchRequest(
        agent_id=AGENT_ID,
        task_id=TASK.id,
        lease_token="l" * 32,
        fencing_token=1,
        lease_expires_at=LEASE_EXPIRY,
        batch_id=batch_id,
        records=records,
    )


JOB = CollectionJob(
    id=UUID("55555555-eeee-4555-8555-555555555555"),
    client_id=CLIENT_ID,
    external_request_id="jobreq_records_unit",
    idempotency_key_hash="sha256:" + "5" * 64,
    request_payload=CollectionJobPayload(
        source=SourceCode.ZHAOPIN,
        queries=[{"keyword": "Python", "region": "Shanghai"}],
    ),
    status=CollectionState.RUNNING,
)
TASK = CollectionTask(
    id=UUID("66666666-ffff-4666-8666-666666666666"),
    collection_job_id=JOB.id,
    source=SourceCode.ZHAOPIN,
    keyword="Python",
    region="Shanghai",
    page_limit=3,
    status=CollectionState.RUNNING,
    leased_agent_id=AGENT_ID,
    fencing_token=1,
    lease_expires_at=LEASE_EXPIRY,
)
PROFILE = BrowserProfile(
    id=UUID("77777777-aaaa-4777-8777-777777777777"),
    agent_id=AGENT_ID,
    source=SourceCode.ZHAOPIN,
    profile_key="primary",
    status="busy",
)
TASK.leased_profile_id = PROFILE.id
AGENT = CollectionAgent(
    id=AGENT_ID,
    device_credential_hash="hmac-sha256:" + "a" * 64,
    display_name="Records Agent",
    status="online",
    current_task_id=TASK.id,
)
ATTEMPT = CollectionTaskAttempt(
    id=ATTEMPT_ID,
    collection_task_id=TASK.id,
    agent_id=AGENT_ID,
    fencing_token=1,
    claimed_at=NOW - timedelta(minutes=1),
)


class StubLeaseAuthenticator:
    async def authenticate(self, **_: object) -> AuthenticatedTaskLease:
        return AuthenticatedTaskLease(
            agent=AGENT,
            job=JOB,
            task=TASK,
            profile=PROFILE,
            attempt=ATTEMPT,
        )


class FakeRecordRepository:
    def __init__(self) -> None:
        self.records: list[StoredParsedJobRecord] = []

    async def insert_many(
        self, records: list[StoredParsedJobRecord], created_at: datetime
    ) -> tuple[list[StoredParsedJobRecord], int]:
        inserted = 0
        stored: list[StoredParsedJobRecord] = []
        for proposed in records:
            existing = next(
                (
                    record
                    for record in self.records
                    if record.collection_task_id == proposed.collection_task_id
                    and record.source_url == proposed.source_url
                    and record.payload_hash == proposed.payload_hash
                ),
                None,
            )
            if existing is None:
                existing = StoredParsedJobRecord(
                    collection_task_id=proposed.collection_task_id,
                    source=proposed.source,
                    source_job_id=proposed.source_job_id,
                    source_url=proposed.source_url,
                    keyword=proposed.keyword,
                    region=proposed.region,
                    page=proposed.page,
                    fields=proposed.fields,
                    payload_hash=proposed.payload_hash,
                    collected_at=proposed.collected_at,
                    id=proposed.id,
                    created_at=created_at,
                )
                self.records.append(existing)
                inserted += 1
            stored.append(existing)
        return stored, inserted

    async def list_for_job(
        self,
        job_id: UUID,
        *,
        after_created_at: datetime | None,
        after_id: UUID | None,
        limit: int,
    ) -> list[StoredParsedJobRecord]:
        records = sorted(self.records, key=lambda item: (item.created_at, item.id))
        if after_created_at is not None and after_id is not None:
            records = [
                item
                for item in records
                if item.created_at is not None
                and (item.created_at, item.id) > (after_created_at, after_id)
            ]
        return records[:limit]


class FakeBatchRepository:
    def __init__(self) -> None:
        self.receipts: dict[UUID, ParsedRecordBatchReceipt] = {}

    async def get(self, batch_id: UUID) -> ParsedRecordBatchReceipt | None:
        return self.receipts.get(batch_id)

    async def try_add(self, receipt: ParsedRecordBatchReceipt) -> bool:
        if receipt.id in self.receipts:
            return False
        self.receipts[receipt.id] = receipt
        return True


class FakeJobRepository:
    def __init__(self, job: CollectionJob = JOB) -> None:
        self.job = job

    async def get_for_client(
        self, job_id: UUID, client_id: UUID
    ) -> CollectionJob | None:
        if self.job.id == job_id and self.job.client_id == client_id:
            return self.job
        return None


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.parsed_job_records = FakeRecordRepository()
        self.parsed_record_batches = FakeBatchRepository()
        self.collection_jobs = FakeJobRepository()
        self.commit_count = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


class StubCallerAuthenticator:
    def __init__(self, client_id: UUID = CLIENT_ID) -> None:
        self.client_id = client_id

    async def authenticate(self, **_: object) -> object:
        return SimpleNamespace(client=SimpleNamespace(id=self.client_id))


def ingestor(unit_of_work: FakeUnitOfWork) -> ParsedRecordBatchIngestor:
    return ParsedRecordBatchIngestor(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        lease_authenticator=StubLeaseAuthenticator(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


async def ingest(
    service: ParsedRecordBatchIngestor, request: ParsedRecordBatchRequest
):
    return await service.ingest(
        session_token=SecretStr("test-session"),
        task_id=TASK.id,
        request=request,
    )


def test_payload_hash_and_batch_duplicate_contract_validation() -> None:
    parsed_fields = fields("Engineer")
    with pytest.raises(ValidationError, match="payloadHash"):
        ParsedJobRecord(
            source=SourceCode.ZHAOPIN,
            source_url="https://jobs.example/invalid",
            keyword="Python",
            region="Shanghai",
            page=1,
            fields=parsed_fields,
            collected_at=NOW,
            payload_hash="sha256:" + "0" * 64,
        )
    duplicate = parsed_record("Engineer")
    with pytest.raises(ValidationError, match="duplicate sourceUrl/payloadHash"):
        batch_request([duplicate, duplicate])


async def test_batch_replay_and_cross_batch_record_deduplication() -> None:
    unit_of_work = FakeUnitOfWork()
    service = ingestor(unit_of_work)
    first_request = batch_request(
        [parsed_record("Engineer"), parsed_record("Architect")]
    )

    first = await ingest(service, first_request)
    replay = await ingest(service, first_request)
    second = await ingest(
        service,
        batch_request(
            [parsed_record("Engineer"), parsed_record("Manager")],
            batch_id=uuid4(),
        ),
    )

    assert replay == first
    assert first.inserted_count == 2
    assert first.duplicate_count == 0
    assert second.inserted_count == 1
    assert second.duplicate_count == 1
    assert second.accepted_record_ids[0] == first.accepted_record_ids[0]
    assert len(unit_of_work.parsed_job_records.records) == 3


async def test_batch_id_content_change_and_task_mismatch_are_rejected() -> None:
    unit_of_work = FakeUnitOfWork()
    service = ingestor(unit_of_work)
    await ingest(service, batch_request([parsed_record("Engineer")]))

    with pytest.raises(ParsedRecordBatchConflict):
        await ingest(service, batch_request([parsed_record("Different")]))
    with pytest.raises(ParsedRecordTaskMismatch):
        await ingest(
            service,
            batch_request(
                [parsed_record("Wrong Region", region="Beijing")],
                batch_id=uuid4(),
            ),
        )


async def test_reader_uses_stable_job_bound_cursor_and_tenant_boundary() -> None:
    unit_of_work = FakeUnitOfWork()
    service = ingestor(unit_of_work)
    await ingest(
        service,
        batch_request(
            [
                parsed_record("One"),
                parsed_record("Two"),
                parsed_record("Three"),
            ]
        ),
    )
    reader = ParsedRecordReader(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=StubCallerAuthenticator(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    first = await reader.list_for_job(
        access_token=SecretStr("caller-token"),
        job_id=JOB.id,
        cursor=None,
        limit=2,
    )
    second = await reader.list_for_job(
        access_token=SecretStr("caller-token"),
        job_id=JOB.id,
        cursor=first.next_cursor,
        limit=2,
    )

    assert len(first.items) == 2
    assert first.next_cursor is not None
    assert len(second.items) == 1
    assert second.next_cursor is None
    assert {item.record_id for item in first.items + second.items} == {
        record.id for record in unit_of_work.parsed_job_records.records
    }

    other_job_id = uuid4()
    with pytest.raises(ParsedRecordCursorInvalid):
        decode_record_cursor(first.next_cursor, other_job_id)
    other_tenant_reader = ParsedRecordReader(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        authenticator=StubCallerAuthenticator(uuid4()),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    with pytest.raises(CollectionJobNotFound):
        await other_tenant_reader.list_for_job(
            access_token=SecretStr("other-token"),
            job_id=JOB.id,
            cursor=None,
            limit=2,
        )


def test_cursor_rejects_malformed_and_naive_values() -> None:
    with pytest.raises(ParsedRecordCursorInvalid):
        decode_record_cursor("not-a-cursor", JOB.id)
    naive = encode_record_cursor(
        RecordCursor(job_id=JOB.id, created_at=NOW.replace(tzinfo=None), record_id=uuid4())
    )
    with pytest.raises(ParsedRecordCursorInvalid):
        decode_record_cursor(naive, JOB.id)


class StubBatchIngestor:
    def __init__(self, conflict: Exception | None = None) -> None:
        self.conflict = conflict

    async def ingest(self, **_: object) -> object:
        if self.conflict is not None:
            raise self.conflict
        return {
            "batchId": str(BATCH_ID),
            "acceptedRecordIds": [str(UUID(int=1))],
            "insertedCount": 1,
            "duplicateCount": 0,
        }


class StubRecordReader:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def list_for_job(self, **_: object) -> ParsedJobRecordPageResponse:
        if self.error is not None:
            raise self.error
        return ParsedJobRecordPageResponse(items=[])


def records_http_app(
    *,
    ingestor: StubBatchIngestor | None = None,
    reader: StubRecordReader | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(agent_tasks_router)
    app.include_router(collection_jobs_router)
    app.state.parsed_record_batch_ingestor = ingestor or StubBatchIngestor()
    app.state.parsed_record_reader = reader or StubRecordReader()
    return app


def batch_json() -> dict[str, object]:
    payload = batch_request([parsed_record("HTTP")]).model_dump(
        mode="json", by_alias=True
    )
    payload["leaseToken"] = "l" * 32
    return payload


async def test_agent_records_http_success_authentication_and_conflict_mapping() -> None:
    path = f"/v1/agent/tasks/{TASK.id}/records"
    headers = {"Authorization": "Bearer agent-session"}
    async with AsyncClient(
        transport=ASGITransport(app=records_http_app()), base_url="http://test"
    ) as client:
        missing = await client.post(path, json=batch_json())
        success = await client.post(path, headers=headers, json=batch_json())
    async with AsyncClient(
        transport=ASGITransport(
            app=records_http_app(
                ingestor=StubBatchIngestor(ParsedRecordBatchConflict())
            )
        ),
        base_url="http://test",
    ) as client:
        conflict = await client.post(path, headers=headers, json=batch_json())

    assert missing.status_code == 401
    assert success.status_code == 200
    assert success.json()["insertedCount"] == 1
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ParsedRecordCursorInvalid(), 400),
        (CallerAccessTokenInsufficientScope(), 403),
        (CollectionJobNotFound(), 404),
    ],
)
async def test_caller_records_http_error_mapping(
    error: Exception, expected_status: int
) -> None:
    app = records_http_app(reader=StubRecordReader(error))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/collection-jobs/{JOB.id}/records",
            headers={"Authorization": "Bearer caller-token"},
        )
    assert response.status_code == expected_status


async def test_caller_records_http_pagination_validation_and_success() -> None:
    app = records_http_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        success = await client.get(
            f"/v1/collection-jobs/{JOB.id}/records?limit=25",
            headers={"Authorization": "Bearer caller-token"},
        )
        invalid_limit = await client.get(
            f"/v1/collection-jobs/{JOB.id}/records?limit=201",
            headers={"Authorization": "Bearer caller-token"},
        )
    assert success.status_code == 200
    assert success.json() == {"items": [], "nextCursor": None}
    assert invalid_limit.status_code == 422

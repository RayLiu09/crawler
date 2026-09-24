import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from crawler_api.domain.entities import CollectionTask
from crawler_api.domain.parsed_records import (
    ParsedRecordBatchReceipt,
    StoredParsedJobRecord,
)
from crawler_contracts.agent_protocol import ParsedRecordBatchRequest
from crawler_contracts.common import ApiScope
from crawler_contracts.parsed_jobs import (
    ParsedJobRecordPageResponse,
    ParsedJobRecordResponse,
    ParsedRecordBatchResponse,
)
from pydantic import HttpUrl, SecretStr

from .caller_authentication import CallerAccessTokenAuthenticator
from .collection_jobs import CollectionJobNotFound
from .ports import UnitOfWork
from .task_leases import ActiveTaskLeaseAuthenticator


class ParsedRecordBatchConflict(Exception):
    """A batch identifier was replayed with different content or task ownership."""


class ParsedRecordTaskMismatch(Exception):
    """One or more records do not match immutable facts in the leased task."""


class ParsedRecordCursorInvalid(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RecordCursor:
    job_id: UUID
    created_at: datetime
    record_id: UUID


class ParsedRecordBatchIngestor:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        lease_authenticator: ActiveTaskLeaseAuthenticator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._lease_authenticator = lease_authenticator
        self._clock = clock

    async def ingest(
        self,
        *,
        session_token: SecretStr,
        task_id: UUID,
        request: ParsedRecordBatchRequest,
    ) -> ParsedRecordBatchResponse:
        received_at = self._clock()
        request_hash = hash_batch_records(request)
        async with self._unit_of_work_factory() as unit_of_work:
            lease = await self._lease_authenticator.authenticate(
                unit_of_work=unit_of_work,
                session_token=session_token,
                expected_task_id=task_id,
                lease_reference=request,
                authenticated_at=received_at,
            )
            self._validate_task_records(lease.task, request)
            existing = await unit_of_work.parsed_record_batches.get(request.batch_id)
            if existing is not None:
                response = self._replay(existing, task_id, request_hash)
                await unit_of_work.commit()
                return response

            proposed = [
                StoredParsedJobRecord(
                    collection_task_id=task_id,
                    source=record.source,
                    source_job_id=record.source_job_id,
                    source_url=str(record.source_url),
                    keyword=record.keyword,
                    region=record.region,
                    page=record.page,
                    fields=record.fields,
                    payload_hash=record.payload_hash,
                    collected_at=record.collected_at,
                    created_at=received_at,
                )
                for record in request.records
            ]
            stored, inserted_count = await unit_of_work.parsed_job_records.insert_many(
                proposed, received_at
            )
            receipt = ParsedRecordBatchReceipt(
                id=request.batch_id,
                collection_task_id=task_id,
                attempt_id=lease.attempt.id,
                request_hash=request_hash,
                record_ids=[record.id for record in stored],
                inserted_count=inserted_count,
                created_at=received_at,
            )
            if not await unit_of_work.parsed_record_batches.try_add(receipt):
                winner = await unit_of_work.parsed_record_batches.get(request.batch_id)
                if winner is None:
                    raise ParsedRecordBatchConflict
                response = self._replay(winner, task_id, request_hash)
            else:
                response = self._response(receipt)
            await unit_of_work.commit()
            return response

    @staticmethod
    def _validate_task_records(
        task: CollectionTask, request: ParsedRecordBatchRequest
    ) -> None:
        if any(
            record.source is not task.source
            or record.keyword != task.keyword
            or record.region != task.region
            or record.page > task.page_limit
            for record in request.records
        ):
            raise ParsedRecordTaskMismatch

    @classmethod
    def _replay(
        cls,
        receipt: ParsedRecordBatchReceipt,
        task_id: UUID,
        request_hash: str,
    ) -> ParsedRecordBatchResponse:
        if (
            receipt.collection_task_id != task_id
            or receipt.request_hash != request_hash
        ):
            raise ParsedRecordBatchConflict
        return cls._response(receipt)

    @staticmethod
    def _response(receipt: ParsedRecordBatchReceipt) -> ParsedRecordBatchResponse:
        return ParsedRecordBatchResponse(
            batch_id=receipt.id,
            accepted_record_ids=receipt.record_ids,
            inserted_count=receipt.inserted_count,
            duplicate_count=len(receipt.record_ids) - receipt.inserted_count,
        )


class ParsedRecordReader:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: CallerAccessTokenAuthenticator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._clock = clock

    async def list_for_job(
        self,
        *,
        access_token: SecretStr,
        job_id: UUID,
        cursor: str | None,
        limit: int,
    ) -> ParsedJobRecordPageResponse:
        read_at = self._clock()
        position = decode_record_cursor(cursor, job_id) if cursor else None
        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_READ,
                authenticated_at=read_at,
            )
            job = await unit_of_work.collection_jobs.get_for_client(
                job_id, caller.client.id
            )
            if job is None:
                raise CollectionJobNotFound
            records = await unit_of_work.parsed_job_records.list_for_job(
                job.id,
                after_created_at=position.created_at if position else None,
                after_id=position.record_id if position else None,
                limit=limit + 1,
            )
            has_more = len(records) > limit
            page_records = records[:limit]
            next_cursor = None
            if has_more and page_records:
                last = page_records[-1]
                if last.created_at is None:
                    raise RuntimeError("persisted parsed record is missing created_at")
                next_cursor = encode_record_cursor(
                    RecordCursor(
                        job_id=job.id,
                        created_at=last.created_at,
                        record_id=last.id,
                    )
                )
            response = ParsedJobRecordPageResponse(
                items=[record_response(record) for record in page_records],
                next_cursor=next_cursor,
            )
            await unit_of_work.commit()
            return response


def hash_batch_records(request: ParsedRecordBatchRequest) -> str:
    canonical = json.dumps(
        [record.model_dump(mode="json", by_alias=True) for record in request.records],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def record_response(record: StoredParsedJobRecord) -> ParsedJobRecordResponse:
    if record.created_at is None:
        raise RuntimeError("persisted parsed record is missing created_at")
    return ParsedJobRecordResponse(
        record_id=record.id,
        task_id=record.collection_task_id,
        source=record.source,
        source_job_id=record.source_job_id,
        source_url=HttpUrl(record.source_url),
        keyword=record.keyword,
        region=record.region,
        page=record.page,
        fields=record.fields,
        collected_at=record.collected_at,
        payload_hash=record.payload_hash,
        created_at=record.created_at,
    )


def encode_record_cursor(cursor: RecordCursor) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "jobId": str(cursor.job_id),
            "createdAt": cursor.created_at.isoformat(),
            "recordId": str(cursor.record_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_record_cursor(value: str, expected_job_id: UUID) -> RecordCursor:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "jobId",
            "createdAt",
            "recordId",
        }:
            raise ValueError
        if payload["v"] != 1:
            raise ValueError
        cursor = RecordCursor(
            job_id=UUID(payload["jobId"]),
            created_at=datetime.fromisoformat(payload["createdAt"]),
            record_id=UUID(payload["recordId"]),
        )
        if cursor.created_at.tzinfo is None or cursor.job_id != expected_job_id:
            raise ValueError
        return cursor
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise ParsedRecordCursorInvalid from error

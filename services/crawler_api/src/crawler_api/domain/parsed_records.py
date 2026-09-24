from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import ParsedJobFields


@dataclass(frozen=True, slots=True)
class StoredParsedJobRecord:
    collection_task_id: UUID
    source: SourceCode
    source_job_id: str | None
    source_url: str
    keyword: str
    region: str
    page: int
    fields: ParsedJobFields
    payload_hash: str
    collected_at: datetime
    id: UUID = field(default_factory=uuid4)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ParsedRecordBatchReceipt:
    collection_task_id: UUID
    attempt_id: UUID
    request_hash: str
    record_ids: list[UUID]
    inserted_count: int
    id: UUID
    created_at: datetime | None = None

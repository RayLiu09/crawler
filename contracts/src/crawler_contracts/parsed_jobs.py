import hashlib
import hmac
import json
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator

from .common import ApiModel, SourceCode


class ParsedJobFields(ApiModel):
    title: str = Field(min_length=1, max_length=300)
    company_name: str | None = Field(default=None, max_length=300)
    address_text: str | None = Field(default=None, max_length=500)
    salary_text: str | None = Field(default=None, max_length=120)
    experience_text: str | None = Field(default=None, max_length=120)
    degree_text: str | None = Field(default=None, max_length=120)
    hiring_text: str | None = Field(default=None, max_length=120)
    description: str | None = None
    skills_text: str | None = None
    job_responsibilities_text: str | None = None
    job_requirements_text: str | None = None
    qualifications_text: str | None = None
    other_matters_text: str | None = None
    company_industry_text: str | None = Field(default=None, max_length=300)
    company_scale_text: str | None = Field(default=None, max_length=120)
    company_financing_text: str | None = Field(default=None, max_length=120)
    job_tags_text: str | None = None


def calculate_payload_hash(fields: ParsedJobFields) -> str:
    canonical = json.dumps(
        fields.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class ParsedJobRecord(ApiModel):
    source: SourceCode
    source_job_id: str | None = Field(default=None, max_length=200)
    source_url: HttpUrl
    keyword: str = Field(min_length=1, max_length=120)
    region: str = Field(default="", max_length=80)
    page: int = Field(ge=1)
    fields: ParsedJobFields
    collected_at: datetime
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record_integrity(self) -> Self:
        if self.collected_at.tzinfo is None:
            raise ValueError("collectedAt must include a timezone")
        expected_hash = calculate_payload_hash(self.fields)
        if not hmac.compare_digest(self.payload_hash, expected_hash):
            raise ValueError("payloadHash does not match the canonical parsed fields")
        return self


class ParsedRecordBatchResponse(ApiModel):
    batch_id: UUID
    accepted_record_ids: list[UUID]
    inserted_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)


class ParsedJobRecordResponse(ParsedJobRecord):
    record_id: UUID
    task_id: UUID
    created_at: datetime


class ParsedJobRecordPageResponse(ApiModel):
    items: list[ParsedJobRecordResponse]
    next_cursor: str | None = None

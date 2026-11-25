import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from crawler_contracts.parsed_jobs import ParsedJobFields, ParsedJobRecord, calculate_payload_hash
from desktop_agent.runtime.spool import LocalSpool
from pydantic import ValidationError

LEGACY_PATH = Path(__file__).parent / "fixtures" / "parsed-job-0.4.0.json"


def test_04_record_preserves_payload_and_batch_serialization() -> None:
    # Generated using the released crawler_contracts-0.4.0 wheel, not this model.
    original = json.loads(LEGACY_PATH.read_text())
    record = ParsedJobRecord.model_validate(original)
    assert record.model_dump(mode="json", by_alias=True) == original
    assert calculate_payload_hash(record.fields) == original["payloadHash"]
    assert record.fields.location_text is None
    assert record.fields.hiring_count is None  # Do not backfill during validation.


def test_04_local_spool_survives_upgrade_and_reupload(tmp_path: Path) -> None:
    original = json.loads(LEGACY_PATH.read_text())
    record = ParsedJobRecord.model_validate(original)
    path = tmp_path / "spool.sqlite3"
    spool = LocalSpool(path)
    spool.enqueue(task_id=uuid4(), batch_id=uuid4(), records=[record])
    restored = LocalSpool(path).pending()[0].records[0]
    assert restored.model_dump(mode="json", by_alias=True) == original


def test_05_fields_roundtrip_and_are_protected_by_payload_hash() -> None:
    original = json.loads(LEGACY_PATH.read_text())
    original["fields"].update(
        {
            "locationText": "湛江·霞山区",
            "hiringCount": 3,
            "publishTimeText": "昨日发布",
            "publishedDate": "2026-11-19",
        }
    )
    with pytest.raises(ValidationError, match="payloadHash"):
        ParsedJobRecord.model_validate(original)
    # Independent canonical hash verifies that the new facts participate.
    canonical = json.dumps(
        original["fields"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    original["payloadHash"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    record = ParsedJobRecord.model_validate(original)
    assert record.model_dump(mode="json", by_alias=True) == original
    assert record.fields.published_date == date(2026, 11, 19)


def test_unknown_optional_fields_do_not_change_legacy_hash() -> None:
    original = json.loads(LEGACY_PATH.read_text())
    original["fields"].update(
        {
            "locationText": None,
            "hiringCount": None,
            "publishTimeText": None,
            "publishedDate": None,
            "publishedAt": None,
        }
    )
    assert ParsedJobRecord.model_validate(original).payload_hash == original["payloadHash"]


def test_publication_time_requires_timezone_and_counts_are_nonnegative_integers() -> None:
    with pytest.raises(ValidationError, match="publishedAt"):
        ParsedJobFields(title="Job", published_at=datetime(2026, 11, 20))
    fields = ParsedJobFields(title="Job", published_at=datetime(2026, 11, 20, tzinfo=UTC))
    assert fields.model_dump(mode="json", by_alias=True)["publishedAt"] == "2026-11-20T00:00:00Z"
    for value in [-1, True, 1.5]:
        with pytest.raises(ValidationError):
            ParsedJobFields.model_validate({"title": "Job", "hiringCount": value})

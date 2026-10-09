from datetime import UTC, datetime

import pytest
from crawler_contracts.parsed_jobs import calculate_payload_hash
from desktop_agent.source_adapters import FakeSourceAdapter

COLLECTED_AT = datetime(2026, 9, 18, 9, 30, tzinfo=UTC)


class RecordingControl:
    def __init__(self, *, stop_on_check: int | None = None) -> None:
        self.check_count = 0
        self.checkpoints: list[tuple[int, int]] = []
        self._stop_on_check = stop_on_check

    def checkpoint(self, page: int, processed_count: int) -> None:
        self.checkpoints.append((page, processed_count))

    def raise_if_control_requested(self) -> None:
        self.check_count += 1
        if self.check_count == self._stop_on_check:
            raise CollectionStopped


class CollectionStopped(Exception):
    pass


def collect_records(*, control: RecordingControl | None = None):
    resolved_control = control or RecordingControl()
    adapter = FakeSourceAdapter(records_per_page=2, clock=lambda: COLLECTED_AT)
    records = list(adapter.collect("Python", "Shanghai", 3, resolved_control))
    return records, resolved_control


def test_same_query_and_clock_produce_identical_records_and_hashes() -> None:
    first, _ = collect_records()
    second, _ = collect_records()

    assert first == second
    assert len(first) == 6
    assert all(record.payload_hash == calculate_payload_hash(record.fields) for record in first)
    assert all(record.collected_at == COLLECTED_AT for record in first)


def test_records_have_unique_stable_source_id_and_url() -> None:
    records, _ = collect_records()

    assert len({record.source_job_id for record in records}) == len(records)
    assert len({str(record.source_url) for record in records}) == len(records)
    assert all(
        str(record.source_url).startswith("https://fake-source.invalid/jobs/")
        for record in records
    )


def test_task_query_values_are_preserved_exactly() -> None:
    adapter = FakeSourceAdapter(records_per_page=1, clock=lambda: COLLECTED_AT)

    record = next(iter(adapter.collect(" Python ", " Shanghai ", 1, RecordingControl())))

    assert record.keyword == " Python "
    assert record.region == " Shanghai "


def test_control_checks_and_page_checkpoints_are_deterministic() -> None:
    _, control = collect_records()

    assert control.check_count == 9
    assert control.checkpoints == [(1, 2), (2, 4), (3, 6)]


def test_100_pages_can_be_collected() -> None:
    control = RecordingControl()
    adapter = FakeSourceAdapter(records_per_page=1, clock=lambda: COLLECTED_AT)

    records = list(adapter.collect("Python", "Shanghai", 100, control))

    assert len(records) == 100
    assert records[-1].page == 100
    assert control.checkpoints[-1] == (100, 100)


def test_control_exception_stops_before_next_record() -> None:
    control = RecordingControl(stop_on_check=3)
    adapter = FakeSourceAdapter(records_per_page=2, clock=lambda: COLLECTED_AT)
    iterator = iter(adapter.collect("Python", "Shanghai", 3, control))
    records = [next(iterator)]

    with pytest.raises(CollectionStopped):
        next(iterator)

    assert len(records) == 1
    assert records[0].page == 1
    assert control.check_count == 3
    assert control.checkpoints == []


@pytest.mark.parametrize(
    ("keyword", "region", "page_limit"),
    [
        ("", "Shanghai", 1),
        ("   ", "Shanghai", 1),
        ("Python", "Shanghai", 0),
        ("Python", "Shanghai", 101),
    ],
)
def test_invalid_collection_query_is_rejected(
    keyword: str, region: str, page_limit: int
) -> None:
    adapter = FakeSourceAdapter(clock=lambda: COLLECTED_AT)

    with pytest.raises(ValueError):
        list(adapter.collect(keyword, region, page_limit, RecordingControl()))


def test_invalid_records_per_page_is_rejected() -> None:
    with pytest.raises(ValueError):
        FakeSourceAdapter(records_per_page=0)


def test_browser_profile_validation_has_no_external_dependency() -> None:
    FakeSourceAdapter(clock=lambda: COLLECTED_AT).validate_browser_profile()

import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import (
    ParsedJobFields,
    ParsedJobRecord,
    calculate_payload_hash,
)
from pydantic import HttpUrl

from .base import CollectionControl

MAX_PAGE_LIMIT = 100


class FakeSourceAdapter:
    """Deterministic source implementation for protocol and runtime tests."""

    source = SourceCode.ZHAOPIN

    def __init__(
        self,
        *,
        records_per_page: int = 2,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if records_per_page < 1:
            raise ValueError("records_per_page must be at least 1")
        self._records_per_page = records_per_page
        self._clock = clock

    def validate_browser_profile(self) -> None:
        """The deterministic adapter deliberately has no browser dependency."""

    def collect(
        self,
        keyword: str,
        region: str,
        page_limit: int,
        control: CollectionControl,
        *,
        start_page: int = 1,
        initial_processed_count: int = 0,
    ) -> Iterable[ParsedJobRecord]:
        if not keyword.strip():
            raise ValueError("keyword must not be empty")
        if not 1 <= page_limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"page_limit must be between 1 and {MAX_PAGE_LIMIT}")
        if not 1 <= start_page <= page_limit + 1 or initial_processed_count < 0:
            raise ValueError("invalid collection resume position")

        processed_count = initial_processed_count
        for page in range(start_page, page_limit + 1):
            control.raise_if_control_requested()
            for position in range(1, self._records_per_page + 1):
                control.raise_if_control_requested()
                yield self._build_record(
                    keyword=keyword,
                    region=region,
                    page=page,
                    position=position,
                )
                processed_count += 1
            control.checkpoint(page, processed_count)

    def _build_record(
        self,
        *,
        keyword: str,
        region: str,
        page: int,
        position: int,
    ) -> ParsedJobRecord:
        identity = f"{keyword}\0{region}\0{page}\0{position}".encode()
        stable_id = hashlib.sha256(identity).hexdigest()[:24]
        fields = ParsedJobFields(
            title=f"{keyword} Engineer {page}-{position}",
            company_name=f"Deterministic Company {stable_id[:8]}",
            address_text=region,
            salary_text=f"{10 + page}k-{20 + position}k",
            experience_text=f"{position}-3 years",
            degree_text="Bachelor",
            description=f"Deterministic {keyword} role in {region}.",
            skills_text=f"{keyword}, deterministic-testing",
            company_industry_text="Software",
            company_scale_text="100-499",
            company_financing_text="Private",
            job_tags_text=f"page-{page},position-{position}",
        )
        return ParsedJobRecord(
            source=self.source,
            source_job_id=f"fake-{stable_id}",
            source_url=HttpUrl(f"https://fake-source.invalid/jobs/{stable_id}"),
            keyword=keyword,
            region=region,
            page=page,
            fields=fields,
            collected_at=self._clock(),
            payload_hash=calculate_payload_hash(fields),
        )

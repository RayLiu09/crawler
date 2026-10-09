from collections.abc import Iterable
from typing import Protocol

from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import ParsedJobRecord


class CollectionControl(Protocol):
    """Control bridge used at explicit Adapter safe points.

    Adapters check before a page and before starting the next record. A checkpoint
    is only authoritative after the Agent runtime has durably accepted every
    record yielded for that page.
    """

    def checkpoint(self, page: int, processed_count: int) -> None: ...

    def raise_if_control_requested(self) -> None: ...


class SourceAdapter(Protocol):
    source: SourceCode

    def validate_browser_profile(self) -> None: ...

    def collect(
        self,
        keyword: str,
        region: str,
        page_limit: int,
        control: CollectionControl,
        *,
        start_page: int = 1,
        initial_processed_count: int = 0,
    ) -> Iterable[ParsedJobRecord]: ...

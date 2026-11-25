import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import (
    ParsedJobFields,
    ParsedJobRecord,
    calculate_payload_hash,
)
from pydantic import HttpUrl

from desktop_agent.browser import BrowserProcessSupervisor
from desktop_agent.browser.base import BrowserDriver, BrowserElement, By
from desktop_agent.browser.errors import BrowserError, BrowserTimeoutError
from desktop_agent.browser.processes import BrowserProcessHandle
from desktop_agent.browser.selenium import create_selenium_browser

from ..base import CollectionControl
from .city_codes import load_city_codes
from .fields import (
    HIRING_TEXT_PATTERN,
    PUBLICATION_TEXT_PATTERN,
    parse_hiring_count,
    parse_publish_time,
)

DEFAULT_MAX_PAGE_LIMIT = 100
LOGGER = logging.getLogger(__name__)
HIRING_COUNT_PATTERN = HIRING_TEXT_PATTERN
COMPANY_SCALE_PATTERN = re.compile(
    r"(?:少于\s*\d+\s*人|\d+\s*[-至]\s*\d+\s*人|\d+\s*人以上|\d+\s*人以下)"
)
DESCRIPTION_SECTION_PATTERN = re.compile(
    r"^(?P<label>岗位职责|工作职责|岗位描述|职位描述|工作内容|岗位要求|职位要求|任职资格|任职要求|"
    r"加分项|加分条件|优先条件|其他事项|其他)\s*[:：]?\s*(?P<inline>.*)$"
)
PANEL_LABEL_PATTERN = re.compile(
    r"^(?P<label>薪资|薪酬|职位薪资|工资|工作经验|经验要求|经验|学历要求|学历|"
    r"工作职责|"
    r"工作地址|工作地点|地址|岗位所在地|所在地区|工作地区|招聘人数|发布时间|发布日期|"
    r"职位描述|岗位描述|岗位职责|工作内容|任职要求|"
    r"职位要求|任职资格|技能要求|岗位要求|加分项|其他事项|其他)\s*[:：]?\s*(?P<value>.*)$"
)
SOURCE_JOB_ID_PATTERNS = (
    re.compile(r"/jobdetail/([^/?#]+)", re.IGNORECASE),
    re.compile(r"/jobs?/[^/?#]+/([^/?#]+)", re.IGNORECASE),
    re.compile(r"[?&]position(?:Id)?=([^&#]+)", re.IGNORECASE),
)


class ZhaopinAdapterError(RuntimeError):
    """Base error for stable Zhaopin adapter failures."""


class BrowserProfileInvalid(ZhaopinAdapterError):
    pass


class UnsupportedZhaopinRegion(ZhaopinAdapterError):
    pass


class ZhaopinPageLoadError(ZhaopinAdapterError):
    pass


class ZhaopinNoResults(ZhaopinAdapterError):
    pass


class ZhaopinLoginRequired(ZhaopinAdapterError):
    """The source requires login and cannot continue in the current mode."""


class ZhaopinVerificationRequired(ZhaopinAdapterError):
    """The source requires a security verification step."""


class ZhaopinLoginTimeout(ZhaopinAdapterError):
    """Manual login did not complete within the configured window."""


class ZhaopinVerificationTimeout(ZhaopinAdapterError):
    """Manual security verification did not complete within the configured window."""


BrowserName = Literal["auto", "chrome", "edge"]
BrowserDriverFactory = Callable[["ZhaopinBrowserConfig"], BrowserDriver]
HumanInterventionState = Literal[
    "login_required",
    "verification_required",
    "resumed",
]
HumanInterventionReporter = Callable[[HumanInterventionState, str], None]


@dataclass(frozen=True, slots=True)
class ZhaopinBrowserConfig:
    profile_directory: Path
    browser_name: BrowserName = "chrome"
    headless: bool = False
    list_timeout_seconds: float = 45
    detail_timeout_seconds: float = 15
    page_load_timeout_seconds: float = 30
    page_delay_seconds: float = 2
    human_intervention_timeout_seconds: float = 900
    human_intervention_poll_seconds: float = 3
    max_page_limit: int = DEFAULT_MAX_PAGE_LIMIT

    def __post_init__(self) -> None:
        if self.list_timeout_seconds < 0:
            raise ValueError("list_timeout_seconds cannot be negative")
        if self.detail_timeout_seconds < 0:
            raise ValueError("detail_timeout_seconds cannot be negative")
        if self.page_load_timeout_seconds <= 0:
            raise ValueError("page_load_timeout_seconds must be positive")
        if self.page_delay_seconds < 0:
            raise ValueError("page_delay_seconds cannot be negative")
        if self.human_intervention_timeout_seconds <= 0:
            raise ValueError("human_intervention_timeout_seconds must be positive")
        if self.human_intervention_poll_seconds <= 0:
            raise ValueError("human_intervention_poll_seconds must be positive")
        if self.human_intervention_poll_seconds > self.human_intervention_timeout_seconds:
            raise ValueError(
                "human_intervention_poll_seconds cannot exceed human_intervention_timeout_seconds"
            )
        if not 1 <= self.max_page_limit <= DEFAULT_MAX_PAGE_LIMIT:
            raise ValueError(f"max_page_limit must be between 1 and {DEFAULT_MAX_PAGE_LIMIT}")


@dataclass(frozen=True, slots=True)
class ZhaopinListing:
    source_url: str
    title: str
    company_name: str
    address_text: str
    salary_text: str
    experience_text: str
    degree_text: str
    company_financing_text: str
    company_scale_text: str
    company_industry_text: str
    job_tags_text: str
    location_text: str = ""


@dataclass(frozen=True, slots=True)
class ZhaopinListingPage:
    """Parsed listings and the number of source cards returned for a page.

    ``listings`` is allowed to be empty even when the source returned cards:
    parsing can fail for a changed card layout.  Pagination must use
    ``source_listing_count`` to decide whether the source reached its end;
    deduplicating parsed listings must never make the current page look empty.
    """

    listings: list[ZhaopinListing]
    source_listing_count: int


@dataclass(frozen=True, slots=True)
class ZhaopinDetail:
    salary_text: str = ""
    experience_text: str = ""
    degree_text: str = ""
    hiring_text: str = ""
    description: str = ""
    skills_text: str = ""
    job_responsibilities_text: str = ""
    job_requirements_text: str = ""
    qualifications_text: str = ""
    other_matters_text: str = ""
    address_text: str = ""
    company_financing_text: str = ""
    company_scale_text: str = ""
    company_industry_text: str = ""
    location_text: str = ""
    publish_time_text: str = ""
    observed_at: datetime | None = None


class ZhaopinSourceAdapter:
    source: SourceCode = SourceCode.ZHAOPIN

    # The source re-renders its result list asynchronously. Element handles
    # obtained before a re-render become detached, so card reads are retried a
    # few times after re-querying the DOM before a listing is skipped.
    STALE_ELEMENT_RETRY_ATTEMPTS = 3
    STALE_ELEMENT_RETRY_DELAY_SECONDS = 0.2

    # The old sou.zhaopin.com card is retained as the first selector because
    # existing Profiles may still be redirected to that legacy page. The new
    # www.zhaopin.com/jobs page has changed card markup, so detail links are a
    # supported fallback and are parsed as lightweight listing elements.
    LIST_ITEM_XPATH = (
        "//div[contains("
        "concat(' ', normalize-space(@class), ' '), "
        "' joblist-box__item '"
        ")]"
    )
    LIST_ITEM_XPATHS = (
        LIST_ITEM_XPATH,
        "//*[contains(@class, 'joblist-box__item')]",
        "//*[contains(@class, 'job-card') and .//a[contains(@href, '/jobdetail/')]]",
        "//*[contains(@class, 'job-card') and .//a[contains(@href, 'positionId=')]]",
    )
    LIST_LINK_XPATH = (
        "//a[contains(@href, '/jobdetail/') or contains(@href, 'positionId=')]"
    )
    JOB_LIST_PANEL_XPATH = (
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' job-list-panel ')]"
    )
    JOB_DETAIL_PANEL_XPATH = (
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' job-detail-panel ')]"
    )
    JOB_LIST_LINK_XPATH = (
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' job-list-panel ')]"
        "//a[contains(@href, '/jobdetail/') or contains(@href, 'positionId=')]"
    )
    JOB_CARD_XPATH = (
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' job-list-panel ')]"
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' job-card ')]"
    )
    JOB_CARD_TITLE_XPATH = (
        ".//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' job-card__title-clamp ')]"
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' vue-clamp__text ')]"
    )
    JOB_CARD_SALARY_XPATH = ".//*[contains(@class, 'job-card__salary')]"
    JOB_CARD_SKILL_XPATH = (
        ".//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' job-card__skill-tag ')]"
    )
    JOB_CARD_COMPANY_XPATH = ".//*[contains(@class, 'job-card__company-name')]"
    JOB_CARD_LOCATION_XPATH = ".//*[contains(@class, 'job-card__location')]//span"
    SUMMARY_TAGS_XPATH = ".//ul[contains(@class, 'job-detail-summary__tags')]//li"
    DETAIL_LINK_XPATH = ".//a[contains(@class, 'job-detail-view-more')]"
    DETAIL_TITLE_XPATHS = (
        ".//span[contains(@class, 'job-detail-summary__title-text')]",
        "//h1[contains(@class, 'summary-planes__title')]",
        ".//h1[contains(@class, 'summary-planes__title')]",
        ".//h1",
        ".//*[contains(@class, 'job-detail-title')]",
        ".//*[contains(@class, 'job-name')]",
        ".//*[contains(@class, 'job-title')]",
    )
    EMPTY_XPATH = (
        "//*[contains(@class, 'page-empty') or contains(@class, 'search-empty') "
        "or contains(@class, 'no-result') or contains(@class, 'empty-result')]"
    )
    EMPTY_TEXT_XPATH = (
        "//*[contains(normalize-space(.), '暂无相关职位') "
        "or contains(normalize-space(.), '暂无职位') "
        "or contains(normalize-space(.), '没有找到相关职位')]"
    )
    TITLE_LINK_XPATH = (
        ".//div[contains(@class, 'jobinfo__name-row')]/a"
        " | .//div[contains(@class, 'jobinfo__top')]/a"
    )
    TITLE_XPATH = "//h1[contains(@class, 'summary-planes__title')]"
    INFO_ITEMS_XPATH = "//ul[contains(@class, 'summary-planes__info')]/li"
    LOGIN_PROMPT_XPATH = (
        "//*[contains(normalize-space(.), '登录之后再搜索')]"
        " | //*[contains(normalize-space(.), '验证码登录/注册')]"
        " | //a[normalize-space(.)='登录/注册']"
        " | //button[normalize-space(.)='登录/注册']"
    )

    def __init__(
        self,
        config: ZhaopinBrowserConfig,
        *,
        driver_factory: BrowserDriverFactory = create_selenium_browser,
        city_codes: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        human_intervention_reporter: HumanInterventionReporter | None = None,
        process_supervisor: BrowserProcessSupervisor | None = None,
    ) -> None:
        resolved_city_codes = load_city_codes() if city_codes is None else city_codes
        if not resolved_city_codes or any(
            not region.strip() or not code.isdecimal()
            for region, code in resolved_city_codes.items()
        ):
            raise ValueError("city_codes must contain non-empty regions and numeric codes")
        self._config = config
        self._driver_factory = driver_factory
        self._city_codes = dict(resolved_city_codes)
        self._clock = clock
        self._sleep = sleeper
        self._monotonic = monotonic
        self._human_intervention_reporter = human_intervention_reporter
        self._process_supervisor = process_supervisor or BrowserProcessSupervisor()
        self._driver: BrowserDriver | None = None
        self._process_handle: BrowserProcessHandle | None = None

    def validate_browser_profile(self) -> None:
        profile_directory = self._config.profile_directory.expanduser()
        try:
            profile_directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BrowserProfileInvalid("browser profile directory cannot be created") from error
        if not profile_directory.is_dir():
            raise BrowserProfileInvalid("browser profile path is not a directory")

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
        self._validate_query(keyword, region, page_limit)
        if not 1 <= start_page <= page_limit + 1 or initial_processed_count < 0:
            raise ValueError("invalid collection resume position")
        if start_page > page_limit:
            return
        self.validate_browser_profile()
        LOGGER.info(
            "zhaopin collection started keyword=%r region=%r page_limit=%d",
            keyword,
            region,
            page_limit,
        )
        if self._driver is None:
            self._driver = self._driver_factory(self._config)
            self._process_handle = self._process_supervisor.track(self._driver)
        driver = self._driver
        processed_count = initial_processed_count
        try:
            for page in range(start_page, page_limit + 1):
                control.raise_if_control_requested()
                listing_page = self._load_listings(driver, keyword, region, page, control)
                listings = listing_page.listings
                LOGGER.info(
                    "zhaopin listing page loaded page=%d region=%r source_listings=%d "
                    "parsed_listings=%d",
                    page,
                    region,
                    listing_page.source_listing_count,
                    len(listings),
                )
                if listing_page.source_listing_count == 0:
                    if processed_count == 0:
                        raise ZhaopinNoResults("search returned no job listings")
                    control.checkpoint(page, processed_count)
                    break

                for listing in listings:
                    control.raise_if_control_requested()
                    detail = self._load_detail(driver, listing, control)
                    record = self._record(keyword, region, page, listing, detail)
                    processed_count += 1
                    yield record

                control.checkpoint(page, processed_count)
                LOGGER.info(
                    "zhaopin page checkpoint page=%d processed_count=%d",
                    page,
                    processed_count,
                )
                if page < page_limit and self._config.page_delay_seconds:
                    self._sleep(self._config.page_delay_seconds)
        finally:
            LOGGER.info(
                "zhaopin collection finished keyword=%r region=%r processed_count=%d",
                keyword,
                region,
                processed_count,
            )
            self.close()

    def close(self) -> None:
        """Close the shared browser after all queries for this task have run."""
        if self._process_handle is not None:
            self._process_handle.close()
        self._process_handle = None
        self._driver = None

    def build_search_url(self, keyword: str, region: str, page: int) -> str:
        normalized_region = region.strip()
        city_code = self._city_codes.get(normalized_region)
        if city_code is None and normalized_region.endswith(("市", "省")):
            city_code = self._city_codes.get(normalized_region[:-1])
        if normalized_region and city_code is None:
            raise UnsupportedZhaopinRegion(f"unsupported Zhaopin region: {region}")
        params = [("pageMode", "search"), ("kw", keyword)]
        if city_code is not None:
            params.insert(0, ("jl", city_code))
        if page > 1:
            params.append(("page", str(page)))
        url = f"https://www.zhaopin.com/jobs?{urlencode(params)}"
        LOGGER.debug(
            "zhaopin search URL built keyword=%r region=%r page=%d url=%s",
            keyword,
            region,
            page,
            url,
        )
        return url

    @staticmethod
    def extract_source_job_id(url: str) -> str | None:
        for pattern in SOURCE_JOB_ID_PATTERNS:
            match = pattern.search(url)
            if match is None:
                continue
            source_job_id = unquote(match.group(1)).strip()
            if 1 <= len(source_job_id) <= 200:
                return source_job_id
        return None

    @staticmethod
    def normalize_job_url(url: str) -> str | None:
        raw_url = url.strip()
        if raw_url.startswith(("/", "//")):
            raw_url = urljoin("https://www.zhaopin.com", raw_url)
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError:
            return None
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or (hostname != "zhaopin.com" and not hostname.endswith(".zhaopin.com"))
            or port not in {None, 80, 443}
        ):
            return None
        netloc = hostname if port in {None, 80, 443} else f"{hostname}:{port}"
        return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))

    def _validate_query(self, keyword: str, region: str, page_limit: int) -> None:
        if not keyword.strip():
            raise ValueError("keyword must not be empty")
        max_page_limit = self._config.max_page_limit
        if not 1 <= page_limit <= max_page_limit:
            raise ValueError(f"page_limit must be between 1 and {max_page_limit}")

    def _load_listings(
        self,
        driver: BrowserDriver,
        keyword: str,
        region: str,
        page: int,
        control: CollectionControl,
    ) -> ZhaopinListingPage:
        search_url = self.build_search_url(keyword, region, page)
        with suppress(BrowserTimeoutError):
            driver.get(search_url)

        self._ensure_page_ready(driver, control=control, return_url=search_url)
        self._wait_until(
            lambda: bool(self._listing_elements(driver) or self._empty_markers(driver)),
            timeout_seconds=self._config.list_timeout_seconds,
            failure_message="Zhaopin listing page did not expose a list or empty marker",
            page_state=(driver, search_url, control),
        )
        elements = self._listing_elements(driver)
        listings, stale = self._parse_listing_elements(elements)
        if stale:
            # A card detached while the page was being read (lazy render or
            # selection repaint). Re-query once after the list settles so a
            # single stale handle cannot drop the whole page.
            self._sleep(self.STALE_ELEMENT_RETRY_DELAY_SECONDS)
            retry_listings, _ = self._parse_listing_elements(
                self._listing_elements(driver)
            )
            seen = {listing.source_url for listing in listings}
            listings.extend(
                listing
                for listing in retry_listings
                if listing.source_url not in seen
            )
        return ZhaopinListingPage(listings=listings, source_listing_count=len(elements))

    def _parse_listing_elements(
        self, elements: Sequence[BrowserElement]
    ) -> tuple[list[ZhaopinListing], bool]:
        """Parse cards, reporting whether any element went stale mid-read."""
        listings: list[ZhaopinListing] = []
        stale = False
        for element in elements:
            try:
                listing = self._parse_listing(element)
            except BrowserError:
                stale = True
                LOGGER.debug("zhaopin listing card went stale while parsing")
                continue
            if listing is not None:
                listings.append(listing)
        return listings, stale

    def _listing_elements(self, driver: BrowserDriver) -> list[BrowserElement]:
        elements: list[BrowserElement] = []
        seen: set[int] = set()
        has_job_list_panel = bool(
            driver.find_elements(By.XPATH, self.JOB_LIST_PANEL_XPATH)
        )

        # A split-panel result exposes both a job card and the anchor nested
        # inside that card. Treat the card as the source result and use the
        # link only when the page has no cards. Keeping both representations
        # would emit the same result twice on one page while still failing to
        # distinguish that DOM duplication from legitimate repeated results
        # across pages.
        if has_job_list_panel:
            cards = driver.find_elements(By.XPATH, self.JOB_CARD_XPATH)
            if cards:
                return list(cards)
            panel_links = driver.find_elements(By.XPATH, self.JOB_LIST_LINK_XPATH)
            if panel_links:
                return list(panel_links)

        panel_links = driver.find_elements(By.XPATH, self.JOB_LIST_LINK_XPATH)
        for element in panel_links:
            marker = id(element)
            if marker not in seen:
                seen.add(marker)
                elements.append(element)
        for element in driver.find_elements(By.XPATH, self.JOB_CARD_XPATH):
            marker = id(element)
            if marker not in seen:
                seen.add(marker)
                elements.append(element)
        for xpath in self.LIST_ITEM_XPATHS:
            for element in driver.find_elements(By.XPATH, xpath):
                marker = id(element)
                if marker not in seen:
                    seen.add(marker)
                    elements.append(element)
        # On the split-panel page, links outside the left list can point to
        # recommendations or the currently displayed detail. Only use the
        # page-wide link fallback for legacy pages without a list panel.
        if not has_job_list_panel:
            for element in driver.find_elements(By.XPATH, self.LIST_LINK_XPATH):
                marker = id(element)
                if marker not in seen:
                    seen.add(marker)
                    elements.append(element)
        return elements

    def _empty_markers(self, driver: BrowserDriver) -> list[BrowserElement]:
        return [
            *driver.find_elements(By.XPATH, self.EMPTY_XPATH),
            *driver.find_elements(By.XPATH, self.EMPTY_TEXT_XPATH),
        ]

    def _load_detail(
        self,
        driver: BrowserDriver,
        listing: ZhaopinListing,
        control: CollectionControl,
    ) -> ZhaopinDetail:
        if self._has_split_search_layout(driver):
            return self._load_split_panel_detail(driver, listing, control)
        return self._load_legacy_detail(driver, listing.source_url, control)

    def _load_split_panel_detail(
        self,
        driver: BrowserDriver,
        listing: ZhaopinListing,
        control: CollectionControl,
    ) -> ZhaopinDetail:
        listing_id = self.extract_source_job_id(listing.source_url)
        if not self._select_listing_card(driver, listing, listing_id):
            LOGGER.warning("zhaopin detail card not found source_url=%s", listing.source_url)
            return ZhaopinDetail()
        try:
            def detail_ready() -> bool:
                panel = self._split_detail_panel(driver)
                if panel is None or not self._detail_title(panel):
                    return False
                detail_url = self._safe_attribute(panel, self.DETAIL_LINK_XPATH, "href")
                detail_id = self.extract_source_job_id(detail_url)
                if listing_id and not listing_id.startswith("card-") and detail_id:
                    return re.sub(r"\.html?$", "", listing_id) == re.sub(r"\.html?$", "", detail_id)
                summary_title = self._safe_text(
                    panel, ".//span[contains(@class, 'job-detail-summary__title-text')]"
                )
                if summary_title:
                    return re.sub(r"\s+", "", summary_title) == re.sub(r"\s+", "", listing.title)
                return True

            self._wait_until(
                detail_ready,
                timeout_seconds=self._config.detail_timeout_seconds,
                failure_message="Zhaopin detail panel did not expose a title",
                page_state=(driver, driver.current_url, control),
            )
            panel = self._split_detail_panel(driver)
            if panel is None:
                return ZhaopinDetail()
            return self._parse_split_detail(panel)
        except (BrowserError, ZhaopinPageLoadError):
            return ZhaopinDetail()

    def _load_legacy_detail(
        self,
        driver: BrowserDriver,
        source_url: str,
        control: CollectionControl,
    ) -> ZhaopinDetail:
        try:
            with suppress(BrowserTimeoutError):
                driver.get(source_url)
            self._ensure_page_ready(driver, control=control, return_url=source_url)
            self._wait_until(
                lambda: self._detail_title(driver) != "",
                timeout_seconds=self._config.detail_timeout_seconds,
                failure_message="Zhaopin detail page did not expose a title",
                page_state=(driver, source_url, control),
            )
        except (BrowserError, ZhaopinPageLoadError):
            return ZhaopinDetail()

        return self._parse_detail(driver)

    def _parse_detail(self, container: BrowserElement) -> ZhaopinDetail:
        observed_at = self._clock()
        info_texts = self._texts(
            self._find_elements_any(
                container,
                (
                    ".//ul[contains(@class, 'summary-planes__info')]/li",
                    self.INFO_ITEMS_XPATH,
                ),
            )
        )
        hiring_text = next(
            (
                match.group(0)
                for text in info_texts
                if (match := HIRING_COUNT_PATTERN.search(text)) is not None
            ),
            "",
        )
        company_info = self._texts(
            self._find_elements_any(
                container,
                (
                    ".//ul[contains(@class, 'company-summary__list')]"
                    "//span[contains(@class, 'company-summary__text')]",
                    "//ul[contains(@class, 'company-summary__list')]"
                    "//span[contains(@class, 'company-summary__text')]",
                ),
            )
        )
        description = self._safe_text_any(
            container,
            (
                ".//div[contains(@class, 'describtion-card__detail-content')]",
                "//div[contains(@class, 'describtion-card__detail-content')]",
                ".//*[contains(@class, 'description')]",
            ),
        )
        sections = self._split_description_sections(description)
        financing, scale, industry = self._parse_company_meta_values(company_info)
        return ZhaopinDetail(
            observed_at=observed_at,
            publish_time_text=next(
                (text for text in info_texts if PUBLICATION_TEXT_PATTERN.fullmatch(text)), ""
            ),
            salary_text=self._safe_text_any(
                container,
                (
                    ".//div[contains(@class, 'summary-planes__left')]/span",
                    "//div[contains(@class, 'summary-planes__left')]/span",
                    ".//*[contains(@class, 'salary')]",
                ),
            ),
            hiring_text=hiring_text,
            description=description,
            skills_text=self._line_list(
                self._safe_text_any(
                    container,
                    (
                        ".//div[contains(@class, 'describtion-card__skills-content')]",
                        "//div[contains(@class, 'describtion-card__skills-content')]",
                        ".//*[contains(@class, 'skills')]",
                    ),
                )
            ),
            address_text=self._safe_text_any(
                container,
                (
                    ".//div[contains(@class, 'address-info__bubble')]",
                    "//div[contains(@class, 'address-info__bubble')]",
                    ".//*[contains(@class, 'address')]",
                ),
            ),
            job_responsibilities_text=sections[0],
            job_requirements_text=sections[1],
            qualifications_text=sections[2],
            other_matters_text=sections[3],
            company_financing_text=financing,
            company_scale_text=scale,
            company_industry_text=industry,
        )

    @staticmethod
    def _parse_company_meta_values(values: Sequence[str]) -> tuple[str, str, str]:
        """Map company metadata when financing is omitted by the source."""
        normalized = [value.strip() for value in values if value.strip()]
        scale = next(
            (value for value in normalized if COMPANY_SCALE_PATTERN.search(value)),
            "",
        )
        remaining = [value for value in normalized if value != scale]
        if not remaining:
            return "", scale, ""
        if len(normalized) >= 3:
            return remaining[0], scale, remaining[-1]
        return "", scale, remaining[0]

    @staticmethod
    def _split_description_sections(text: str) -> tuple[str, str, str, str]:
        """Extract structured sections while retaining the original description."""
        sections: dict[str, list[str]] = {
            "responsibilities": [],
            "requirements": [],
            "qualifications": [],
            "other": [],
        }
        current: list[str] | None = None
        lines = [part.strip() for part in text.splitlines() if part.strip()]
        for line in lines:
            if not line:
                continue
            match = DESCRIPTION_SECTION_PATTERN.match(line)
            if match is not None:
                label = match.group("label")
                if label in {"岗位职责", "工作职责", "岗位描述", "职位描述", "工作内容"}:
                    current = sections["responsibilities"]
                elif label in {"岗位要求", "职位要求"}:
                    current = sections["requirements"]
                elif label in {"任职资格", "任职要求"}:
                    current = sections["qualifications"]
                else:
                    current = sections["other"]
                inline = match.group("inline").strip()
                if inline:
                    current.append(inline)
                continue
            if current is not None:
                current.append(line)
        # Some source pages omit section headings and expose only numbered or
        # bulleted responsibility text. Preserve that content in the closest
        # structured field instead of dropping it from parsed fields.
        if not any(sections.values()) and lines and any(
            re.match(r"^(?:\d+\s*[、.．)、)]|[-*•●▪])", line) for line in lines
        ):
            bullet = re.compile(r"^(?:\d+\s*[、.．)、)]|[-*•●▪])")
            first_non_bullet = next(
                (index for index, line in enumerate(lines) if not bullet.match(line)),
                len(lines),
            )
            sections["responsibilities"].extend(lines[:first_non_bullet])
            if first_non_bullet < len(lines):
                sections["other"].extend(lines[first_non_bullet:])
        return (
            "\n".join(sections["responsibilities"]).strip(),
            "\n".join(sections["requirements"]).strip(),
            "\n".join(sections["qualifications"]).strip(),
            "\n".join(sections["other"]).strip(),
        )

    def _parse_split_detail(self, panel: BrowserElement) -> ZhaopinDetail:
        """Parse the current split-panel DOM, then fall back to visible labels."""
        observed_at = self._clock()
        summary_tags = self._texts(panel.find_elements(By.XPATH, self.SUMMARY_TAGS_XPATH))
        location = self._summary_value(panel, "address")
        experience = self._summary_value(panel, "workexp")
        degree = self._summary_value(panel, "education")
        hiring = self._summary_value(panel, "renshu") or next(
            (text for text in summary_tags if HIRING_COUNT_PATTERN.fullmatch(text)), ""
        )
        publish_time = self._summary_value(panel, "time") or next(
            (text for text in summary_tags if PUBLICATION_TEXT_PATTERN.fullmatch(text)), ""
        )
        location = location or self._summary_label_value(
            panel, ("岗位所在地", "所在地区", "工作地区")
        )
        hiring = hiring or self._summary_label_value(panel, ("招聘人数",))
        publish_time = publish_time or self._summary_label_value(panel, ("发布时间", "发布日期"))
        if not location and summary_tags:
            first_tag = summary_tags[0]
            if (
                not HIRING_COUNT_PATTERN.fullmatch(first_tag)
                and not PUBLICATION_TEXT_PATTERN.fullmatch(first_tag)
                and not re.search(
                    r"经验|学历|年|本科|大专|硕士|博士|高中|中专|初中|全职|兼职", first_tag
                )
            ):
                location = first_tag
        if not experience:
            experience = next(
                (text for text in summary_tags if re.search(r"经验|\d+\s*[-至]\s*\d+\s*年", text)),
                "",
            )
        if not degree:
            degree = next(
                (
                    text for text in summary_tags
                    if re.search(r"学历|本科|大专|硕士|博士|高中|中专|初中", text)
                ),
                "",
            )
        description = self._safe_text_any(
            panel,
            (".//div[contains(@class, 'job-description__content')]",),
        )
        skills = self._texts(
            panel.find_elements(
                By.XPATH,
                ".//div[contains(@class, 'job-description__tags')]"
                "//span[contains(@class, 'job-description__tag')]",
            )
        )
        salary = self._safe_text_any(
            panel,
            (".//span[contains(@class, 'job-detail-summary__salary')]",),
        )
        address = self._safe_text_any(
            panel,
            (".//div[contains(@class, 'job-detail-address__bubble')]",),
        )
        company_meta = self._safe_text_any(
            panel,
            (".//*[contains(@class, 'job-detail-summary__company-meta')]",),
        )
        company_values = [
            value.strip()
            for value in company_meta.strip().lstrip("/").split("·")
            if value.strip()
        ]
        if summary_tags or description or skills or salary or address or company_meta:
            structured_sections = self._split_description_sections(description)
            financing, scale, industry = self._parse_company_meta_values(company_values)
            return ZhaopinDetail(
                observed_at=observed_at,
                location_text=location,
                hiring_text=hiring,
                publish_time_text=publish_time,
                salary_text=salary,
                experience_text=experience,
                degree_text=degree,
                description=description,
                skills_text=", ".join(skills),
                job_responsibilities_text=structured_sections[0],
                job_requirements_text=structured_sections[1],
                qualifications_text=structured_sections[2],
                other_matters_text=structured_sections[3],
                address_text=address or location,
                company_financing_text=financing,
                company_scale_text=scale,
                company_industry_text=industry,
            )

        # Some deployments render the same panel with class names deferred;
        # retain a text-label fallback for that transient markup.
        lines = [line.strip() for line in panel.text.splitlines() if line.strip()]
        if not lines:
            return self._parse_detail(panel)

        values: dict[str, str] = {}
        sections: dict[str, str] = {}
        section_labels = {
            "职位描述": "description",
            "岗位描述": "description",
            "岗位职责": "description",
            "工作职责": "description",
            "工作内容": "description",
            "任职要求": "skills",
            "任职资格": "qualifications",
            "技能要求": "skills",
            "岗位要求": "skills",
            "职位要求": "requirements",
            "加分项": "other",
            "其他事项": "other",
            "其他": "other",
        }
        current_section: str | None = None
        section_lines: list[str] = []
        for line in lines:
            match = PANEL_LABEL_PATTERN.match(line)
            if match is not None:
                if current_section is not None:
                    sections[current_section] = "\n".join(section_lines).strip()
                label = match.group("label")
                value = match.group("value").strip()
                current_section = section_labels.get(label)
                section_lines = [value] if current_section is not None and value else []
                if current_section is None:
                    values[label] = value
                continue
            if current_section is not None:
                section_lines.append(line)
        if current_section is not None:
            sections[current_section] = "\n".join(section_lines).strip()

        description_start = next(
            (
                index
                for index, line in enumerate(lines)
                if (match := DESCRIPTION_SECTION_PATTERN.match(line)) is not None
                and match.group("label")
                in {
                    "岗位职责",
                    "工作职责",
                    "岗位描述",
                    "职位描述",
                    "工作内容",
                    "岗位要求",
                    "职位要求",
                    "任职资格",
                    "任职要求",
                    "加分项",
                    "加分条件",
                    "优先条件",
                    "其他事项",
                    "其他",
                }
            ),
            len(lines),
        )
        hiring_text = next(
            (line for line in lines[:description_start] if HIRING_COUNT_PATTERN.search(line)), ""
        ) or hiring
        return ZhaopinDetail(
            observed_at=observed_at,
            location_text=location,
            publish_time_text=publish_time or next(
                (line for line in lines[:description_start]
                 if PUBLICATION_TEXT_PATTERN.fullmatch(line)), ""
            ),
            salary_text=self._first_value(values, ("薪资", "薪酬", "职位薪资", "工资")),
            hiring_text=hiring_text,
            description="\n".join(lines[description_start:]),
            skills_text=self._line_list(sections.get("skills", "")),
            job_responsibilities_text=sections.get("description", ""),
            job_requirements_text=sections.get("requirements", ""),
            qualifications_text=sections.get("qualifications", ""),
            other_matters_text=sections.get("other", ""),
            address_text=self._first_value(values, ("工作地址", "工作地点", "地址")),
        )

    @staticmethod
    def _summary_label_value(panel: BrowserElement, labels: Sequence[str]) -> str:
        text = ZhaopinSourceAdapter._safe_text(
            panel, ".//*[contains(@class, 'job-detail-summary__info')]"
        ) or panel.text
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        pattern = re.compile(r"^(?:" + "|".join(map(re.escape, labels)) + r")\s*[:：]?\s*(.*)$")
        for index, line in enumerate(lines):
            if DESCRIPTION_SECTION_PATTERN.fullmatch(line):
                break
            match = pattern.fullmatch(line)
            if match:
                return match.group(1) or (lines[index + 1] if index + 1 < len(lines) else "")
        return ""

    @staticmethod
    def _summary_value(panel: BrowserElement, icon: str) -> str:
        return ZhaopinSourceAdapter._safe_text(
            panel,
            ".//ul[contains(@class, 'job-detail-summary__tags')]"
            f"/li[.//img[contains(@src, '/ic-{icon}.')]]/span",
        )

    @staticmethod
    def _first_value(values: Mapping[str, str], labels: Sequence[str]) -> str:
        return next((values[label] for label in labels if values.get(label)), "")

    def _select_listing_card(
        self,
        driver: BrowserDriver,
        listing: ZhaopinListing,
        listing_id: str | None,
    ) -> bool:
        """Select ``listing`` in the split panel, tolerating re-rendered cards.

        The list re-renders asynchronously (lazy detail load, selection
        repaint), detaching WebElement handles returned moments earlier. Each
        attempt re-queries the DOM and skips cards that go stale mid-parse so
        one detached handle cannot abort the whole collection. Dispatch on the
        card itself so a nested detail anchor cannot open a new browser tab.
        """
        for attempt in range(self.STALE_ELEMENT_RETRY_ATTEMPTS):
            stale = False
            for element in driver.find_elements(By.XPATH, self.JOB_CARD_XPATH):
                try:
                    parsed = self._parse_listing(element)
                except BrowserError:
                    stale = True
                    LOGGER.debug("zhaopin listing card went stale while matching")
                    continue
                if parsed is None or not self._listing_matches(parsed, listing, listing_id):
                    continue
                try:
                    driver.execute_script("arguments[0].click()", element)
                except BrowserError:
                    stale = True
                    break
                return True
            if not stale:
                return False
            if attempt + 1 < self.STALE_ELEMENT_RETRY_ATTEMPTS:
                self._sleep(self.STALE_ELEMENT_RETRY_DELAY_SECONDS)
        return False

    @staticmethod
    def _listing_matches(
        parsed: ZhaopinListing,
        listing: ZhaopinListing,
        listing_id: str | None,
    ) -> bool:
        return parsed.source_url == listing.source_url or (
            listing_id is not None
            and ZhaopinSourceAdapter.extract_source_job_id(parsed.source_url) == listing_id
        )

    def _has_split_search_layout(self, driver: BrowserDriver) -> bool:
        return bool(driver.find_elements(By.XPATH, self.JOB_LIST_PANEL_XPATH))

    def _split_detail_panel(self, driver: BrowserDriver) -> BrowserElement | None:
        panels = driver.find_elements(By.XPATH, self.JOB_DETAIL_PANEL_XPATH)
        return panels[0] if panels else None

    def _detail_title(self, container: BrowserElement) -> str:
        for xpath in self.DETAIL_TITLE_XPATHS:
            value = self._safe_text(container, xpath)
            if value:
                return value
        try:
            return next((line.strip() for line in container.text.splitlines() if line.strip()), "")
        except BrowserError:
            return ""

    def _parse_listing(self, element: BrowserElement) -> ZhaopinListing | None:
        is_new_card = bool(
            self._safe_text(element, self.JOB_CARD_TITLE_XPATH)
            or self._safe_text(element, self.JOB_CARD_SALARY_XPATH)
        )
        title = (
            self._safe_text(element, self.JOB_CARD_TITLE_XPATH)
            if is_new_card
            else self._safe_text(element, self.TITLE_LINK_XPATH)
        )
        href = (
            self._safe_attribute(element, self.TITLE_LINK_XPATH, "href")
            if not is_new_card
            else self._safe_direct_attribute(element, "href")
        )
        if not title:
            # New search cards can expose the job detail anchor as the element
            # returned by LIST_LINK_XPATH rather than as a child of a card.
            title = element.text.strip()
            href = self._safe_direct_attribute(element, "href")
        source_url = self.normalize_job_url(href)
        if source_url is None and is_new_card:
            position_id = next(
                (
                    self._safe_direct_attribute(element, name)
                    for name in (
                        "data-position-id",
                        "data-positionid",
                        "data-job-id",
                        "data-jobid",
                        "data-id",
                    )
                    if self._safe_direct_attribute(element, name)
                ),
                "",
            )
            if position_id:
                source_url = self.normalize_job_url(
                    f"https://www.zhaopin.com/jobdetail/{position_id}"
                )
            if source_url is None:
                # Some Vue renders expose only a click handler on the card and
                # omit both href and position-id attributes. Keep a stable
                # per-card identity so the card can still be clicked and
                # its right-hand detail panel can be collected.
                identity = "|".join(
                    (
                        title,
                        self._safe_text(element, self.JOB_CARD_SALARY_XPATH),
                        self._safe_text(element, self.JOB_CARD_COMPANY_XPATH),
                        self._safe_text(element, self.JOB_CARD_LOCATION_XPATH),
                        "\n".join(
                            self._texts(
                                element.find_elements(By.XPATH, self.JOB_CARD_SKILL_XPATH)
                            )
                        ),
                    )
                ).encode("utf-8")
                card_id = hashlib.sha256(identity).hexdigest()[:32]
                source_url = f"https://www.zhaopin.com/jobdetail/card-{card_id}"
        if not title or source_url is None:
            return None

        if is_new_card:
            skills = self._texts(element.find_elements(By.XPATH, self.JOB_CARD_SKILL_XPATH))
            company_name = self._safe_text(element, self.JOB_CARD_COMPANY_XPATH)
            location_parts = self._texts(
                element.find_elements(By.XPATH, self.JOB_CARD_LOCATION_XPATH)
            )
            location = " ".join(location_parts) or self._safe_text(
                element, self.JOB_CARD_LOCATION_XPATH
            )
            degree = skills[0] if skills and skills[0] in {"大专", "本科", "硕士", "博士"} else ""
            experience = next(
                (value for value in skills if re.fullmatch(r"\d+[-至]\d+年|经验不限", value)),
                "",
            )
            job_tags = [value for value in skills if value not in {degree, experience}]
            return ZhaopinListing(
                source_url=source_url,
                title=title,
                company_name=company_name,
                address_text=location,
                location_text=location,
                salary_text=self._safe_text(element, self.JOB_CARD_SALARY_XPATH),
                experience_text=experience,
                degree_text=degree,
                company_financing_text="",
                company_scale_text="",
                company_industry_text="",
                job_tags_text=", ".join(job_tags),
            )

        other_items = self._texts(
            element.find_elements(
                By.XPATH, ".//div[contains(@class, 'jobinfo__other-info-item')]"
            )
        )
        company_tags = self._safe_text(
            element, ".//div[contains(@class, 'companyinfo__tag')]"
        ).splitlines()
        financing, scale, industry = self._parse_company_meta_values(company_tags)
        return ZhaopinListing(
            source_url=source_url,
            title=title,
            company_name=self._safe_text(
                element, ".//div[contains(@class, 'companyinfo__top')]/a"
            ),
            address_text=other_items[0] if len(other_items) > 0 else "",
            location_text=other_items[0] if len(other_items) > 0 else "",
            experience_text=other_items[1] if len(other_items) > 1 else "",
            degree_text=other_items[2] if len(other_items) > 2 else "",
            salary_text=self._safe_text(
                element, ".//p[contains(@class, 'jobinfo__salary')]"
            ),
            company_financing_text=financing,
            company_scale_text=scale,
            company_industry_text=industry,
            job_tags_text=self._line_list(
                self._safe_text(element, ".//div[contains(@class, 'jobinfo__tag')]")
            ),
        )

    def _record(
        self,
        keyword: str,
        region: str,
        page: int,
        listing: ZhaopinListing,
        detail: ZhaopinDetail,
    ) -> ParsedJobRecord:
        collected_at = detail.observed_at or self._clock()
        published_date, published_at = parse_publish_time(detail.publish_time_text, collected_at)
        fields = ParsedJobFields(
            title=listing.title[:300],
            company_name=self._limited(listing.company_name, 300),
            address_text=self._limited(detail.address_text or listing.address_text, 500),
            salary_text=self._limited(listing.salary_text or detail.salary_text, 120),
            experience_text=self._limited(
                listing.experience_text or detail.experience_text, 120
            ),
            degree_text=self._limited(listing.degree_text or detail.degree_text, 120),
            hiring_text=self._limited(detail.hiring_text, 120),
            location_text=self._limited(detail.location_text or listing.location_text, 500),
            hiring_count=parse_hiring_count(detail.hiring_text),
            publish_time_text=self._limited(detail.publish_time_text, 120),
            published_date=published_date,
            published_at=published_at,
            description=detail.description or None,
            skills_text=detail.skills_text or None,
            job_responsibilities_text=detail.job_responsibilities_text or None,
            job_requirements_text=detail.job_requirements_text or None,
            qualifications_text=detail.qualifications_text or None,
            other_matters_text=detail.other_matters_text or None,
            company_industry_text=self._limited(
                listing.company_industry_text or detail.company_industry_text, 300
            ),
            company_scale_text=self._limited(
                listing.company_scale_text or detail.company_scale_text, 120
            ),
            company_financing_text=self._limited(
                listing.company_financing_text or detail.company_financing_text, 120
            ),
            job_tags_text=listing.job_tags_text or None,
        )
        return ParsedJobRecord(
            source=self.source,
            source_job_id=self.extract_source_job_id(listing.source_url),
            source_url=HttpUrl(listing.source_url),
            keyword=keyword,
            region=region,
            page=page,
            fields=fields,
            collected_at=collected_at,
            payload_hash=calculate_payload_hash(fields),
        )

    def _wait_until(
        self,
        condition: Callable[[], bool],
        *,
        timeout_seconds: float,
        failure_message: str,
        page_state: tuple[BrowserDriver, str, CollectionControl] | None = None,
    ) -> None:
        deadline = self._monotonic() + timeout_seconds
        while True:
            if page_state is not None:
                driver, return_url, control = page_state
                self._ensure_page_ready(
                    driver, return_url=return_url, control=control
                )
            if condition():
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ZhaopinPageLoadError(failure_message)
            self._sleep(min(0.2, remaining))

    def _ensure_page_ready(
        self,
        driver: BrowserDriver,
        *,
        return_url: str,
        control: CollectionControl | None = None,
    ) -> None:
        """Keep one visible Profile available for a human source-site intervention."""
        while True:
            if self._is_security_verification(driver):
                self._wait_for_human_intervention(
                    driver,
                    kind="verification",
                    return_url=return_url,
                    control=control,
                )
                continue
            if self._is_login_required(driver):
                self._wait_for_human_intervention(
                    driver,
                    kind="login",
                    return_url=return_url,
                    control=control,
                )
                continue
            return

    def _wait_for_human_intervention(
        self,
        driver: BrowserDriver,
        *,
        kind: Literal["login", "verification"],
        return_url: str,
        control: CollectionControl | None,
    ) -> None:
        if self._config.headless:
            if kind == "login":
                raise ZhaopinLoginRequired(
                    "Zhaopin login is required but the browser is headless"
                )
            raise ZhaopinVerificationRequired(
                "Zhaopin security verification is required but the browser is headless"
            )

        state: HumanInterventionState = (
            "login_required" if kind == "login" else "verification_required"
        )
        self._report_human_intervention(state, driver.current_url)
        deadline = self._monotonic() + self._config.human_intervention_timeout_seconds
        while True:
            if control is not None:
                control.raise_if_control_requested()
            if not (
                self._is_login_required(driver)
                if kind == "login"
                else self._is_security_verification(driver)
            ):
                if kind == "login" and driver.current_url != return_url:
                    with suppress(BrowserTimeoutError):
                        driver.get(return_url)
                    self._wait_until(
                        lambda: not self._is_login_required(driver),
                        timeout_seconds=self._config.detail_timeout_seconds,
                        failure_message="Zhaopin page remained on login after manual login",
                    )
                self._report_human_intervention("resumed", driver.current_url)
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                if kind == "login":
                    raise ZhaopinLoginTimeout(
                        "Zhaopin login did not complete within the configured timeout"
                    )
                raise ZhaopinVerificationTimeout(
                    "Zhaopin security verification did not complete within the configured timeout"
                )
            self._sleep(min(self._config.human_intervention_poll_seconds, remaining))

    def _report_human_intervention(
        self, state: HumanInterventionState, url: str
    ) -> None:
        if self._human_intervention_reporter is not None:
            self._human_intervention_reporter(state, url)

    @classmethod
    def _is_login_required(cls, driver: BrowserDriver) -> bool:
        current_url = cls._driver_url(driver).lower()
        login_path = urlsplit(current_url).path
        if "passport.zhaopin.com" in current_url or re.search(
            r"/login(?:/|$)", login_path
        ):
            return True
        try:
            return bool(driver.find_elements(By.XPATH, cls.LOGIN_PROMPT_XPATH))
        except BrowserError:
            return False

    @staticmethod
    def _is_security_verification(driver: BrowserDriver) -> bool:
        title = ZhaopinSourceAdapter._driver_title(driver).lower()
        source = ZhaopinSourceAdapter._driver_page_source(driver)
        source_lower = source.lower()
        return (
            "security verification" in title
            or "teocaptchawidget" in source_lower
            or "tencenteocaptchawidget" in source_lower
            or "captcha.eo.gtimg.com" in source_lower
            or "traceguard" in source_lower
            or "正在验证连接安全性" in source
            or "请勾选下方复选框" in source
        )

    @staticmethod
    def _driver_url(driver: BrowserDriver) -> str:
        try:
            return driver.current_url
        except BrowserError:
            return ""

    @staticmethod
    def _driver_title(driver: BrowserDriver) -> str:
        try:
            return driver.title
        except BrowserError:
            return ""

    @staticmethod
    def _driver_page_source(driver: BrowserDriver) -> str:
        try:
            return driver.page_source
        except BrowserError:
            return ""

    @staticmethod
    def _safe_text(container: BrowserElement, xpath: str) -> str:
        try:
            return container.find_element(By.XPATH, xpath).text.strip()
        except BrowserError:
            return ""

    @staticmethod
    def _safe_text_any(container: BrowserElement, xpaths: Sequence[str]) -> str:
        for xpath in xpaths:
            value = ZhaopinSourceAdapter._safe_text(container, xpath)
            if value:
                return value
        return ""

    @staticmethod
    def _find_elements_any(
        container: BrowserElement, xpaths: Sequence[str]
    ) -> list[BrowserElement]:
        for xpath in xpaths:
            elements = list(container.find_elements(By.XPATH, xpath))
            if elements:
                return elements
        return []

    @staticmethod
    def _safe_attribute(container: BrowserElement, xpath: str, name: str) -> str:
        try:
            return (container.find_element(By.XPATH, xpath).get_attribute(name) or "").strip()
        except BrowserError:
            return ""

    @staticmethod
    def _safe_direct_attribute(element: BrowserElement, name: str) -> str:
        try:
            return (element.get_attribute(name) or "").strip()
        except BrowserError:
            return ""

    @staticmethod
    def _texts(elements: Sequence[BrowserElement]) -> list[str]:
        return [text for element in elements if (text := element.text.strip())]

    @staticmethod
    def _line_list(value: str) -> str:
        return ", ".join(part.strip() for part in value.splitlines() if part.strip())

    @staticmethod
    def _limited(value: str, maximum: int) -> str | None:
        stripped = value.strip()
        return stripped[:maximum] or None

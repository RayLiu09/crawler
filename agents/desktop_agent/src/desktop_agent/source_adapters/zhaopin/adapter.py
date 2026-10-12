import hashlib
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import (
    ParsedJobFields,
    ParsedJobRecord,
    calculate_payload_hash,
)
from pydantic import HttpUrl
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchDriverException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

from desktop_agent.browser import BrowserProcessSupervisor

from ..base import CollectionControl
from .city_codes import load_city_codes

DEFAULT_MAX_PAGE_LIMIT = 100
LOGGER = logging.getLogger(__name__)
HIRING_COUNT_PATTERN = re.compile(r"招\s*\d+\s*人")
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
    r"工作地址|工作地点|地址|招聘人数|职位描述|岗位描述|岗位职责|工作内容|任职要求|"
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


class ZhaopinLoginRequired(ZhaopinAdapterError):
    """The source requires login and cannot continue in the current mode."""


class ZhaopinVerificationRequired(ZhaopinAdapterError):
    """The source requires a security verification step."""


class ZhaopinLoginTimeout(ZhaopinAdapterError):
    """Manual login did not complete within the configured window."""


class ZhaopinVerificationTimeout(ZhaopinAdapterError):
    """Manual security verification did not complete within the configured window."""


class BrowserElement(Protocol):
    @property
    def text(self) -> str: ...

    def find_element(self, by: str, value: str) -> "BrowserElement": ...

    def find_elements(self, by: str, value: str) -> Sequence["BrowserElement"]: ...

    def get_attribute(self, name: str) -> str | None: ...

    def click(self) -> None: ...


class BrowserDriver(BrowserElement, Protocol):
    @property
    def current_url(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def page_source(self) -> str: ...

    def get(self, url: str) -> None: ...

    def execute_script(self, script: str, *args: object) -> object: ...

    def implicitly_wait(self, time_to_wait: float) -> None: ...

    def set_page_load_timeout(self, time_to_wait: float) -> None: ...

    def quit(self) -> None: ...


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


def _browser_candidates(browser_name: BrowserName) -> tuple[Literal["chrome", "edge"], ...]:
    if browser_name == "auto":
        return ("chrome", "edge") if sys.platform == "win32" else ("chrome",)
    return (browser_name,)


def _configure_options(
    options: webdriver.ChromeOptions | webdriver.EdgeOptions,
    config: ZhaopinBrowserConfig,
) -> None:
    options.add_argument(f"--user-data-dir={config.profile_directory.expanduser().resolve()}")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.page_load_strategy = "eager"
    if config.headless:
        options.add_argument("--headless=new")


def create_zhaopin_webdriver(config: ZhaopinBrowserConfig) -> BrowserDriver:
    last_error: NoSuchDriverException | None = None
    for browser_name in _browser_candidates(config.browser_name):
        try:
            if browser_name == "edge":
                edge_options = webdriver.EdgeOptions()
                _configure_options(edge_options, config)
                edge_driver = webdriver.Edge(options=edge_options)
                edge_driver.implicitly_wait(0.2)
                edge_driver.set_page_load_timeout(config.page_load_timeout_seconds)
                return cast(BrowserDriver, edge_driver)
            chrome_options = webdriver.ChromeOptions()
            _configure_options(chrome_options, config)
            chrome_driver = webdriver.Chrome(options=chrome_options)
            chrome_driver.implicitly_wait(0.2)
            chrome_driver.set_page_load_timeout(config.page_load_timeout_seconds)
            return cast(BrowserDriver, chrome_driver)
        except NoSuchDriverException as error:
            last_error = error
    raise ZhaopinAdapterError("no configured Chrome or Edge WebDriver is available") from last_error


class ZhaopinSourceAdapter:
    source: SourceCode = SourceCode.ZHAOPIN

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
        driver_factory: BrowserDriverFactory = create_zhaopin_webdriver,
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
        self._process_handle = None

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
        seen_urls: set[str] = set()
        try:
            for page in range(start_page, page_limit + 1):
                control.raise_if_control_requested()
                listings = self._load_listings(
                    driver, keyword, region, page, seen_urls, control
                )
                LOGGER.info(
                    "zhaopin listing page loaded page=%d region=%r listings=%d",
                    page,
                    region,
                    len(listings),
                )
                if not listings:
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

    def close(self) -> None:
        """Close the shared browser after all queries for this task have run."""
        if self._process_handle is not None:
            self._process_handle.close()
        self._process_handle = None
        self._driver = None

    def build_search_url(self, keyword: str, region: str, page: int) -> str:
        normalized_region = region.strip()
        city_code = self._city_codes.get(normalized_region)
        if city_code is None and normalized_region.endswith("市"):
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
        seen_urls: set[str],
        control: CollectionControl,
    ) -> list[ZhaopinListing]:
        search_url = self.build_search_url(keyword, region, page)
        with suppress(TimeoutException):
            driver.get(search_url)

        self._ensure_page_ready(driver, control=control, return_url=search_url)
        self._wait_until(
            lambda: bool(self._listing_elements(driver) or self._empty_markers(driver)),
            timeout_seconds=self._config.list_timeout_seconds,
            failure_message="Zhaopin listing page did not expose a list or empty marker",
            page_state=(driver, search_url, control),
        )
        listings: list[ZhaopinListing] = []
        for element in self._listing_elements(driver):
            listing = self._parse_listing(element)
            if listing is None:
                continue
            identity = self.extract_source_job_id(listing.source_url) or listing.source_url
            if identity in seen_urls:
                continue
            seen_urls.add(identity)
            listings.append(listing)
        return listings

    def _listing_elements(self, driver: BrowserDriver) -> list[BrowserElement]:
        elements: list[BrowserElement] = []
        seen: set[int] = set()
        has_job_list_panel = bool(
            driver.find_elements(By.XPATH, self.JOB_LIST_PANEL_XPATH)
        )
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
        cards = driver.find_elements(By.XPATH, self.JOB_CARD_XPATH)
        listing_id = self.extract_source_job_id(listing.source_url)
        target = next(
            (
                element
                for element in cards
                if (
                    parsed := self._parse_listing(element)
                ) is not None
                and (
                    parsed.source_url == listing.source_url
                    or (
                        listing_id is not None
                        and self.extract_source_job_id(parsed.source_url) == listing_id
                    )
                )
            ),
            None,
        )
        if target is None:
            LOGGER.warning("zhaopin detail card not found source_url=%s", listing.source_url)
            return ZhaopinDetail()
        try:
            # Dispatch on the card itself so a nested detail anchor cannot
            # turn the list selection into a new browser tab.
            driver.execute_script("arguments[0].click()", target)

            def detail_ready() -> bool:
                panel = self._split_detail_panel(driver)
                return panel is not None and bool(self._detail_title(panel))

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
        except (WebDriverException, ZhaopinPageLoadError):
            return ZhaopinDetail()

    def _load_legacy_detail(
        self,
        driver: BrowserDriver,
        source_url: str,
        control: CollectionControl,
    ) -> ZhaopinDetail:
        try:
            with suppress(TimeoutException):
                driver.get(source_url)
            self._ensure_page_ready(driver, control=control, return_url=source_url)
            self._wait_until(
                lambda: self._detail_title(driver) != "",
                timeout_seconds=self._config.detail_timeout_seconds,
                failure_message="Zhaopin detail page did not expose a title",
                page_state=(driver, source_url, control),
            )
        except (WebDriverException, ZhaopinPageLoadError):
            return ZhaopinDetail()

        return self._parse_detail(driver)

    def _parse_detail(self, container: BrowserElement) -> ZhaopinDetail:
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
        summary_tags = self._texts(
            panel.find_elements(
                By.XPATH,
                ".//ul[contains(@class, 'job-detail-summary__tags')]//li",
            )
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
                salary_text=salary,
                experience_text=summary_tags[1] if len(summary_tags) > 1 else "",
                degree_text=summary_tags[2] if len(summary_tags) > 2 else "",
                description=description,
                skills_text=", ".join(skills),
                job_responsibilities_text=structured_sections[0],
                job_requirements_text=structured_sections[1],
                qualifications_text=structured_sections[2],
                other_matters_text=structured_sections[3],
                address_text=address or (summary_tags[0] if summary_tags else ""),
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

        hiring_text = next((line for line in lines if HIRING_COUNT_PATTERN.search(line)), "")
        if not hiring_text:
            hiring_text = values.get("招聘人数", "")
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
        return ZhaopinDetail(
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
    def _first_value(values: Mapping[str, str], labels: Sequence[str]) -> str:
        return next((values[label] for label in labels if values.get(label)), "")

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
        except WebDriverException:
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
            location = self._safe_text(element, self.JOB_CARD_LOCATION_XPATH)
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
            collected_at=self._clock(),
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
                    with suppress(TimeoutException):
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
        except WebDriverException:
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
        except WebDriverException:
            return ""

    @staticmethod
    def _driver_title(driver: BrowserDriver) -> str:
        try:
            return driver.title
        except WebDriverException:
            return ""

    @staticmethod
    def _driver_page_source(driver: BrowserDriver) -> str:
        try:
            return driver.page_source
        except WebDriverException:
            return ""

    @staticmethod
    def _safe_text(container: BrowserElement, xpath: str) -> str:
        try:
            return container.find_element(By.XPATH, xpath).text.strip()
        except WebDriverException:
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
        except WebDriverException:
            return ""

    @staticmethod
    def _safe_direct_attribute(element: BrowserElement, name: str) -> str:
        try:
            return (element.get_attribute(name) or "").strip()
        except WebDriverException:
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

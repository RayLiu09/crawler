from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import calculate_payload_hash
from desktop_agent.source_adapters import SourceAdapter
from desktop_agent.source_adapters.zhaopin import (
    BrowserProfileInvalid,
    UnsupportedZhaopinRegion,
    ZhaopinBrowserConfig,
    ZhaopinLoginRequired,
    ZhaopinLoginTimeout,
    ZhaopinSourceAdapter,
    ZhaopinVerificationRequired,
    ZhaopinVerificationTimeout,
)
from selenium.common.exceptions import NoSuchElementException

COLLECTED_AT = datetime(2026, 9, 18, 11, 0, tzinfo=UTC)


class FakeElement:
    def __init__(
        self,
        text: str = "",
        *,
        attributes: dict[str, str] | None = None,
        children: dict[str, "FakeElement"] | None = None,
        child_lists: dict[str, Sequence["FakeElement"]] | None = None,
        title: str = "",
        page_source: str = "",
        on_click: Callable[[], None] | None = None,
    ) -> None:
        self._text = text
        self._attributes = attributes or {}
        self._children = children or {}
        self._child_lists = child_lists or {}
        self._title = title
        self._page_source = page_source
        self._on_click = on_click

    @property
    def text(self) -> str:
        return self._text

    def find_element(self, _by: str, value: str) -> "FakeElement":
        try:
            return self._children[value]
        except KeyError as error:
            raise NoSuchElementException(value) from error

    def find_elements(self, _by: str, value: str) -> Sequence["FakeElement"]:
        return self._child_lists.get(value, ())

    def get_attribute(self, name: str) -> str | None:
        return self._attributes.get(name)

    def click(self) -> None:
        if self._on_click is not None:
            self._on_click()

    @property
    def title(self) -> str:
        return self._title

    @property
    def page_source(self) -> str:
        return self._page_source


class FakeDriver(FakeElement):
    def __init__(self, pages: dict[str, FakeElement]) -> None:
        super().__init__()
        self._pages = pages
        self._current_page = FakeElement()
        self._current_url = "about:blank"
        self.visited_urls: list[str] = []
        self.quit_called = False

    @property
    def current_url(self) -> str:
        return self._current_url

    @property
    def text(self) -> str:
        return self._current_page.text

    @property
    def title(self) -> str:
        return self._current_page.title

    @property
    def page_source(self) -> str:
        return self._current_page.page_source

    def get(self, url: str) -> None:
        self.visited_urls.append(url)
        self._current_url = url
        self._current_page = self._pages.get(url, FakeElement())

    def find_element(self, by: str, value: str) -> FakeElement:
        return self._current_page.find_element(by, value)

    def find_elements(self, by: str, value: str) -> Sequence[FakeElement]:
        return self._current_page.find_elements(by, value)

    def get_attribute(self, name: str) -> str | None:
        return self._current_page.get_attribute(name)

    def implicitly_wait(self, _time_to_wait: float) -> None:
        pass

    def set_page_load_timeout(self, _time_to_wait: float) -> None:
        pass

    def quit(self) -> None:
        self.quit_called = True

    def replace_current_page(self, page: FakeElement, *, url: str | None = None) -> None:
        self._current_page = page
        if url is not None:
            self._current_url = url


class RecordingControl:
    def __init__(self, *, stop_on_check: int | None = None) -> None:
        self.control_checks = 0
        self.checkpoints: list[tuple[int, int]] = []
        self._stop_on_check = stop_on_check

    def raise_if_control_requested(self) -> None:
        self.control_checks += 1
        if self.control_checks == self._stop_on_check:
            raise CollectionStopped

    def checkpoint(self, page: int, processed_count: int) -> None:
        self.checkpoints.append((page, processed_count))


class CollectionStopped(Exception):
    pass


def listing_item(url: str) -> FakeElement:
    return FakeElement(
        children={
            ZhaopinSourceAdapter.TITLE_LINK_XPATH: FakeElement(
                "Python 开发工程师", attributes={"href": url}
            ),
            ".//div[contains(@class, 'companyinfo__top')]/a": FakeElement(
                "示例科技"
            ),
            ".//p[contains(@class, 'jobinfo__salary')]": FakeElement("20K-30K"),
            ".//div[contains(@class, 'companyinfo__tag')]": FakeElement(
                "已上市\n1000-9999人\n互联网"
            ),
            ".//div[contains(@class, 'jobinfo__tag')]": FakeElement(
                "五险一金\n周末双休"
            ),
        },
        child_lists={
            ".//div[contains(@class, 'jobinfo__other-info-item')]": [
                FakeElement("上海-浦东新区"),
                FakeElement("3-5年"),
                FakeElement("本科"),
            ]
        },
    )


def detail_page() -> FakeElement:
    return FakeElement(
        children={
            ZhaopinSourceAdapter.TITLE_XPATH: FakeElement("高级 Python 开发工程师"),
            "//div[contains(@class, 'summary-planes__left')]/span": FakeElement(
                "21K-31K"
            ),
            "//div[contains(@class, 'describtion-card__detail-content')]": FakeElement(
                "负责平台服务开发与维护。"
            ),
            "//div[contains(@class, 'describtion-card__skills-content')]": FakeElement(
                "Python\nPostgreSQL"
            ),
            "//div[contains(@class, 'address-info__bubble')]": FakeElement(
                "上海市浦东新区示例路 1 号"
            ),
        },
        child_lists={
            ZhaopinSourceAdapter.TITLE_XPATH: [FakeElement("高级 Python 开发工程师")],
            ZhaopinSourceAdapter.INFO_ITEMS_XPATH: [
                FakeElement("3-5年"),
                FakeElement("本科"),
                FakeElement("招 2 人"),
            ],
            "//ul[contains(@class, 'company-summary__list')]"
            "//span[contains(@class, 'company-summary__text')]": [
                FakeElement("民营"),
                FakeElement("500-999人"),
                FakeElement("软件服务"),
            ],
        },
    )


def adapter_with_driver(
    tmp_path: Path,
    driver: FakeDriver,
    *,
    detail_timeout_seconds: float = 0,
    human_intervention_timeout_seconds: float = 900,
    human_intervention_poll_seconds: float = 3,
    headless: bool = False,
    human_intervention_reporter=None,
    monotonic=None,
    sleeper=None,
) -> ZhaopinSourceAdapter:
    return ZhaopinSourceAdapter(
        ZhaopinBrowserConfig(
            profile_directory=tmp_path / "profile",
            list_timeout_seconds=0,
            detail_timeout_seconds=detail_timeout_seconds,
            page_delay_seconds=0,
            human_intervention_timeout_seconds=human_intervention_timeout_seconds,
            human_intervention_poll_seconds=human_intervention_poll_seconds,
            headless=headless,
        ),
        driver_factory=lambda _config: driver,
        city_codes={"上海": "538"},
        clock=lambda: COLLECTED_AT,
        sleeper=sleeper or (lambda _seconds: None),
        human_intervention_reporter=human_intervention_reporter,
        **({"monotonic": monotonic} if monotonic is not None else {}),
    )


def test_adapter_satisfies_shared_source_protocol(tmp_path: Path) -> None:
    adapter: SourceAdapter = adapter_with_driver(tmp_path, FakeDriver({}))

    assert adapter.source is SourceCode.ZHAOPIN


def test_collect_maps_listing_and_detail_to_parsed_record(tmp_path: Path) -> None:
    listing_url = "http://jobs.zhaopin.com/jobdetail/CC123.htm?ref=list#position"
    normalized_url = "https://jobs.zhaopin.com/jobdetail/CC123.htm?ref=list"
    first_page_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    second_page_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python&page=2"
    driver = FakeDriver(
        {
            first_page_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.LIST_ITEM_XPATH: [listing_item(listing_url)]
                }
            ),
            normalized_url: detail_page(),
            second_page_url: FakeElement(
                child_lists={ZhaopinSourceAdapter.EMPTY_XPATH: [FakeElement()]}
            ),
        }
    )
    control = RecordingControl()
    adapter = adapter_with_driver(tmp_path, driver)

    records = list(adapter.collect("Python", "上海", 2, control))

    assert len(records) == 1
    record = records[0]
    assert record.source is SourceCode.ZHAOPIN
    assert record.source_job_id == "CC123.htm"
    assert str(record.source_url) == normalized_url
    assert record.keyword == "Python"
    assert record.region == "上海"
    assert record.page == 1
    assert record.collected_at == COLLECTED_AT
    assert record.payload_hash == calculate_payload_hash(record.fields)
    assert record.fields.model_dump() == {
        "title": "Python 开发工程师",
        "company_name": "示例科技",
        "address_text": "上海市浦东新区示例路 1 号",
        "salary_text": "20K-30K",
        "experience_text": "3-5年",
        "degree_text": "本科",
        "hiring_text": "招 2 人",
        "description": "负责平台服务开发与维护。",
        "skills_text": "Python, PostgreSQL",
        "job_responsibilities_text": None,
        "job_requirements_text": None,
        "qualifications_text": None,
        "other_matters_text": None,
        "company_industry_text": "互联网",
        "company_scale_text": "1000-9999人",
        "company_financing_text": "已上市",
        "job_tags_text": "五险一金, 周末双休",
    }
    assert control.control_checks == 3
    assert control.checkpoints == [(1, 1), (2, 1)]
    assert driver.visited_urls == [first_page_url, normalized_url, second_page_url]
    assert driver.quit_called is True


def test_new_search_page_falls_back_to_job_detail_links(tmp_path: Path) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    listing_url = "https://www.zhaopin.com/jobdetail/NEW-DOM"
    driver = FakeDriver(
        {
            search_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.LIST_LINK_XPATH: [
                        FakeElement("Python 后端工程师", attributes={"href": listing_url})
                    ]
                }
            ),
            listing_url: detail_page(),
        }
    )

    records = list(
        adapter_with_driver(tmp_path, driver).collect("Python", "上海", 1, RecordingControl())
    )

    assert len(records) == 1
    assert records[0].source_job_id == "NEW-DOM"
    assert records[0].fields.title == "Python 后端工程师"
    assert driver.visited_urls == [search_url, listing_url]


def test_split_search_layout_ignores_detail_links_outside_job_list_panel(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    listed_url = "https://www.zhaopin.com/jobdetail/LISTED"
    noise_url = (
        "https://www.zhaopin.com/jobdetail/CCL1480117890J40848681905.htm"
        "?srccode=401901&data_identity=f0e8449c419f46e49abbd0c1c22daa8b"
    )
    listed_link = FakeElement("Python 平台工程师", attributes={"href": listed_url})
    noise_link = FakeElement("推荐岗位", attributes={"href": noise_url})
    driver = FakeDriver(
        {
            search_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.JOB_LIST_PANEL_XPATH: [FakeElement()],
                    ZhaopinSourceAdapter.JOB_LIST_LINK_XPATH: [listed_link],
                    ZhaopinSourceAdapter.LIST_LINK_XPATH: [noise_link],
                }
            )
        }
    )
    driver.get(search_url)

    listings = adapter_with_driver(tmp_path, driver)._listing_elements(driver)

    assert listings == [listed_link]


def test_new_card_wrapper_can_fall_back_to_nested_detail_link(tmp_path: Path) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    listing_url = "https://www.zhaopin.com/jobdetail/NEW-WRAPPER"
    driver = FakeDriver(
        {
            search_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.LIST_ITEM_XPATHS[2]: [FakeElement()],
                    ZhaopinSourceAdapter.LIST_LINK_XPATH: [
                        FakeElement("Python 测试工程师", attributes={"href": listing_url})
                    ],
                }
            ),
            listing_url: detail_page(),
        }
    )

    records = list(
        adapter_with_driver(tmp_path, driver).collect("Python", "上海", 1, RecordingControl())
    )

    assert len(records) == 1
    assert records[0].source_job_id == "NEW-WRAPPER"


def test_split_search_layout_clicks_left_item_and_reads_right_detail_panel(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    listing_url = "https://www.zhaopin.com/jobdetail/SPLIT-PANEL"
    clicked = False

    def mark_clicked() -> None:
        nonlocal clicked
        clicked = True

    detail_panel = detail_page()
    listing_link = FakeElement(
        "Python 平台工程师",
        attributes={"href": listing_url},
        on_click=mark_clicked,
    )
    driver = FakeDriver(
        {
            search_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.JOB_LIST_PANEL_XPATH: [FakeElement()],
                    ZhaopinSourceAdapter.JOB_DETAIL_PANEL_XPATH: [detail_panel],
                    ZhaopinSourceAdapter.JOB_LIST_LINK_XPATH: [listing_link],
                }
            )
        }
    )

    records = list(
        adapter_with_driver(tmp_path, driver).collect("Python", "上海", 1, RecordingControl())
    )

    assert len(records) == 1
    assert records[0].source_job_id == "SPLIT-PANEL"
    assert records[0].fields.description == "负责平台服务开发与维护。"
    assert clicked is True
    assert driver.visited_urls == [search_url]


def test_current_split_dom_parses_job_card_and_company_meta_without_legacy_classes(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    clicked = False

    def mark_clicked() -> None:
        nonlocal clicked
        clicked = True

    card = FakeElement(
        attributes={"data-position-id": "POSITION-42"},
        on_click=mark_clicked,
        children={
            ZhaopinSourceAdapter.JOB_CARD_TITLE_XPATH: FakeElement("python开发工程师"),
            ZhaopinSourceAdapter.JOB_CARD_SALARY_XPATH: FakeElement("9000-13000元"),
            ZhaopinSourceAdapter.JOB_CARD_COMPANY_XPATH: FakeElement(
                "西安软玥信息技术有限公司"
            ),
            ZhaopinSourceAdapter.JOB_CARD_LOCATION_XPATH: FakeElement("西安 雁塔 丈八沟"),
        },
        child_lists={
            ZhaopinSourceAdapter.JOB_CARD_SKILL_XPATH: [
                FakeElement("本科"),
                FakeElement("1-3年"),
                FakeElement("Django"),
                FakeElement("Flask"),
            ]
        },
    )
    detail_panel = FakeElement(
        child_lists={
            ".//span[contains(@class, 'job-detail-summary__title-text')]": [
                FakeElement("python开发工程师")
            ],
            ".//span[contains(@class, 'job-detail-summary__salary')]": [
                FakeElement("9000-13000元")
            ],
            ".//ul[contains(@class, 'job-detail-summary__tags')]//li": [
                FakeElement("西安·雁塔区"),
                FakeElement("1-3年"),
                FakeElement("本科"),
            ],
            ".//div[contains(@class, 'job-description__tags')]"
            "//span[contains(@class, 'job-description__tag')]": [
                FakeElement("Django"),
                FakeElement("Flask"),
            ],
        },
        children={
            ".//span[contains(@class, 'job-detail-summary__title-text')]": FakeElement(
                "python开发工程师"
            ),
            ".//div[contains(@class, 'job-description__content')]": FakeElement(
                "工作职责\n参与后端开发"
            ),
            ".//div[contains(@class, 'job-detail-address__bubble')]": FakeElement(
                "西安雁塔区和发智能大厦B座"
            ),
            ".//*[contains(@class, 'job-detail-summary__company-meta')]": FakeElement(
                "/已上市 · 10000人以上 · 软件/IT服务、软件/IT服务、人工智能"
            ),
        },
    )
    driver = FakeDriver(
        {
            search_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.JOB_LIST_PANEL_XPATH: [FakeElement()],
                    ZhaopinSourceAdapter.JOB_DETAIL_PANEL_XPATH: [detail_panel],
                    ZhaopinSourceAdapter.JOB_CARD_XPATH: [card],
                }
            )
        }
    )

    records = list(
        adapter_with_driver(tmp_path, driver).collect("Python", "上海", 1, RecordingControl())
    )

    assert clicked is True
    assert driver.visited_urls == [search_url]
    assert len(records) == 1
    assert records[0].source_job_id == "POSITION-42"
    assert records[0].fields.title == "python开发工程师"
    assert records[0].fields.salary_text == "9000-13000元"
    assert records[0].fields.experience_text == "1-3年"
    assert records[0].fields.degree_text == "本科"
    assert records[0].fields.address_text == "西安雁塔区和发智能大厦B座"
    assert records[0].fields.skills_text == "Django, Flask"
    assert records[0].fields.job_responsibilities_text is None
    assert records[0].fields.job_requirements_text is None
    assert records[0].fields.qualifications_text is None
    assert records[0].fields.other_matters_text is None
    assert records[0].fields.company_financing_text == "已上市"
    assert records[0].fields.company_scale_text == "10000人以上"
    assert records[0].fields.company_industry_text == "软件/IT服务、软件/IT服务、人工智能"


def test_current_job_card_without_href_or_position_id_gets_stable_identity(
    tmp_path: Path,
) -> None:
    card = FakeElement(
        children={
            ZhaopinSourceAdapter.JOB_CARD_TITLE_XPATH: FakeElement("Python 后端工程师"),
            ZhaopinSourceAdapter.JOB_CARD_SALARY_XPATH: FakeElement("9000-13000元"),
        }
    )

    listing = adapter_with_driver(tmp_path, FakeDriver({}))._parse_listing(card)

    assert listing is not None
    assert listing.source_url.startswith("https://www.zhaopin.com/jobdetail/card-")


def test_split_detail_panel_parses_labeled_text_without_legacy_detail_classes(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    listing_url = "https://www.zhaopin.com/jobdetail/SPLIT-TEXT"
    panel = FakeElement(
        text=(
            "Python 数据工程师\n"
            "薪资：25K-35K\n"
            "经验要求：3-5年\n"
            "学历：本科\n"
            "工作地点：上海\n"
            "招聘人数：招 2 人\n"
            "职位描述\n负责数据平台建设\n"
            "任职要求\nPython\nSQL"
        )
    )
    link = FakeElement("Python 数据工程师", attributes={"href": listing_url})
    driver = FakeDriver(
        {
            search_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.JOB_LIST_PANEL_XPATH: [FakeElement()],
                    ZhaopinSourceAdapter.JOB_DETAIL_PANEL_XPATH: [panel],
                    ZhaopinSourceAdapter.JOB_LIST_LINK_XPATH: [link],
                }
            )
        }
    )

    records = list(
        adapter_with_driver(tmp_path, driver).collect("Python", "上海", 1, RecordingControl())
    )

    assert records[0].fields.salary_text == "25K-35K"
    assert records[0].fields.address_text == "上海"
    assert records[0].fields.hiring_text == "招聘人数：招 2 人"
    assert records[0].fields.description == (
        "职位描述\n负责数据平台建设\n任职要求\nPython\nSQL"
    )
    assert records[0].fields.skills_text == "Python, SQL"


def test_new_search_page_accepts_text_only_empty_marker(tmp_path: Path) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=ppcc"
    driver = FakeDriver(
        {
            search_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.EMPTY_TEXT_XPATH: [FakeElement("暂无相关职位")]
                }
            )
        }
    )

    records = list(
        adapter_with_driver(tmp_path, driver).collect("ppcc", "上海", 1, RecordingControl())
    )

    assert records == []
    assert driver.quit_called is True


def test_detail_timeout_keeps_valid_listing_facts(tmp_path: Path) -> None:
    listing_url = "https://jobs.zhaopin.com/jobdetail/PARTIAL"
    first_page_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    driver = FakeDriver(
        {
            first_page_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.LIST_ITEM_XPATH: [listing_item(listing_url)]
                }
            ),
            listing_url: FakeElement(),
        }
    )
    control = RecordingControl()

    records = list(adapter_with_driver(tmp_path, driver).collect("Python", "上海", 1, control))

    assert len(records) == 1
    assert records[0].fields.title == "Python 开发工程师"
    assert records[0].fields.address_text == "上海-浦东新区"
    assert records[0].fields.description is None
    assert control.checkpoints == [(1, 1)]
    assert driver.quit_called is True


def test_control_exception_closes_browser_before_detail_navigation(tmp_path: Path) -> None:
    listing_url = "https://jobs.zhaopin.com/jobdetail/STOPPED"
    first_page_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    driver = FakeDriver(
        {
            first_page_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.LIST_ITEM_XPATH: [listing_item(listing_url)]
                }
            )
        }
    )

    with pytest.raises(CollectionStopped):
        list(
            adapter_with_driver(tmp_path, driver).collect(
                "Python", "上海", 1, RecordingControl(stop_on_check=2)
            )
        )

    assert driver.visited_urls == [first_page_url]
    assert driver.quit_called is True


def test_control_after_detail_keeps_current_record_and_stops_next_page(
    tmp_path: Path,
) -> None:
    listing_url = "https://jobs.zhaopin.com/jobdetail/FINISHED"
    first_page_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    second_page_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python&page=2"
    driver = FakeDriver(
        {
            first_page_url: FakeElement(
                child_lists={
                    ZhaopinSourceAdapter.LIST_ITEM_XPATH: [listing_item(listing_url)]
                }
            ),
            listing_url: detail_page(),
            second_page_url: FakeElement(
                child_lists={ZhaopinSourceAdapter.EMPTY_XPATH: [FakeElement()]}
            ),
        }
    )
    control = RecordingControl(stop_on_check=3)
    iterator = iter(adapter_with_driver(tmp_path, driver).collect("Python", "上海", 2, control))
    records = [next(iterator)]

    with pytest.raises(CollectionStopped):
        next(iterator)

    assert len(records) == 1
    assert records[0].source_job_id == "FINISHED"
    assert control.checkpoints == [(1, 1)]
    assert driver.visited_urls == [first_page_url, listing_url]
    assert driver.quit_called is True


def test_job_url_normalization_rejects_external_or_unsafe_targets() -> None:
    assert (
        ZhaopinSourceAdapter.normalize_job_url(
            "http://jobs.zhaopin.com:80/jobdetail/ABC?positionId=ABC#fragment"
        )
        == "https://jobs.zhaopin.com/jobdetail/ABC?positionId=ABC"
    )
    assert (
        ZhaopinSourceAdapter.normalize_job_url("/jobdetail/RELATIVE")
        == "https://www.zhaopin.com/jobdetail/RELATIVE"
    )
    assert ZhaopinSourceAdapter.normalize_job_url("https://evilzhaopin.com/job/1") is None
    assert ZhaopinSourceAdapter.normalize_job_url("https://zhaopin.com.evil.test/job/1") is None
    assert ZhaopinSourceAdapter.normalize_job_url("https://user@zhaopin.com/job/1") is None
    assert ZhaopinSourceAdapter.normalize_job_url("https://zhaopin.com:8443/job/1") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.zhaopin.com/jobdetail/CC123.htm", "CC123.htm"),
        ("https://sou.zhaopin.com/?positionId=ABC%201", "ABC 1"),
        ("https://sou.zhaopin.com/?other=value", None),
    ],
)
def test_source_job_id_extraction(url: str, expected: str | None) -> None:
    assert ZhaopinSourceAdapter.extract_source_job_id(url) == expected


def test_search_url_uses_only_injected_verified_city_codes(tmp_path: Path) -> None:
    adapter = ZhaopinSourceAdapter(
        ZhaopinBrowserConfig(profile_directory=tmp_path / "profile"),
        city_codes={"上海": "538", "西安": "854", "西安市": "854"},
        driver_factory=lambda _config: FakeDriver({}),
    )

    assert (
        adapter.build_search_url("Python 开发", " 上海 ", 3)
        == "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python+%E5%BC%80%E5%8F%91&page=3"
    )
    assert (
        adapter.build_search_url("Java", "西安市", 1)
        == "https://www.zhaopin.com/jobs?jl=854&pageMode=search&kw=Java"
    )
    assert (
        adapter.build_search_url("Java", "上海市", 1)
        == "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Java"
    )
    assert (
        adapter.build_search_url("Java", "", 1)
        == "https://www.zhaopin.com/jobs?pageMode=search&kw=Java"
    )
    with pytest.raises(UnsupportedZhaopinRegion):
        adapter.build_search_url("Python", "不存在市", 1)

    with pytest.raises(ValueError):
        ZhaopinSourceAdapter(
            ZhaopinBrowserConfig(profile_directory=tmp_path / "invalid-city-code"),
            city_codes={"上海": "538&kw=override"},
        )


def test_company_meta_supports_omitted_financing() -> None:
    assert ZhaopinSourceAdapter._parse_company_meta_values(
        ["20-99人", "财务/审计/税务"]
    ) == ("", "20-99人", "财务/审计/税务")
    assert ZhaopinSourceAdapter._parse_company_meta_values(
        ["已上市", "10000人以上", "软件/IT服务、人工智能"]
    ) == ("已上市", "10000人以上", "软件/IT服务、人工智能")


def test_description_sections_are_split_without_losing_full_text() -> None:
    description = (
        "岗位职责\n负责后端开发\n"
        "岗位要求\n熟悉 Python\n"
        "任职要求\n本科及以上\n"
        "加分项\n有爬虫经验"
    )
    sections = ZhaopinSourceAdapter._split_description_sections(description)
    assert sections == (
        "负责后端开发",
        "熟悉 Python",
        "本科及以上",
        "有爬虫经验",
    )


def test_browser_profile_directory_is_created_and_file_path_is_rejected(
    tmp_path: Path,
) -> None:
    profile_directory = tmp_path / "profiles" / "zhaopin-primary"
    adapter = ZhaopinSourceAdapter(
        ZhaopinBrowserConfig(profile_directory=profile_directory),
        driver_factory=lambda _config: FakeDriver({}),
    )

    adapter.validate_browser_profile()
    assert profile_directory.is_dir()

    invalid_path = tmp_path / "not-a-directory"
    invalid_path.write_text("profile", encoding="utf-8")
    invalid_adapter = ZhaopinSourceAdapter(
        ZhaopinBrowserConfig(profile_directory=invalid_path),
        driver_factory=lambda _config: FakeDriver({}),
    )
    with pytest.raises(BrowserProfileInvalid):
        invalid_adapter.validate_browser_profile()


@pytest.mark.parametrize(
    ("keyword", "region", "page_limit"),
    [
        ("", "上海", 1),
        ("Python", "上海", 0),
        ("Python", "上海", 31),
    ],
)
def test_invalid_query_is_rejected_before_starting_browser(
    tmp_path: Path, keyword: str, region: str, page_limit: int
) -> None:
    driver_started = False

    def driver_factory(_config: ZhaopinBrowserConfig) -> FakeDriver:
        nonlocal driver_started
        driver_started = True
        return FakeDriver({})

    adapter = ZhaopinSourceAdapter(
        ZhaopinBrowserConfig(profile_directory=tmp_path / "profile"),
        driver_factory=driver_factory,
    )

    with pytest.raises(ValueError):
        list(adapter.collect(keyword, region, page_limit, RecordingControl()))
    assert driver_started is False


def login_prompt_page() -> FakeElement:
    return FakeElement(
        text="登录之后再搜索",
        child_lists={
            ZhaopinSourceAdapter.LOGIN_PROMPT_XPATH: [FakeElement("登录之后再搜索")]
        },
    )


def verification_page(*, title: str = "", source: str = "") -> FakeElement:
    return FakeElement(title=title, page_source=source)


def test_login_url_detection_covers_passport_and_login_paths(tmp_path: Path) -> None:
    adapter = adapter_with_driver(tmp_path, FakeDriver({}))
    driver = FakeDriver({})
    driver.get("https://passport.zhaopin.com/login")
    assert adapter._is_login_required(driver) is True
    driver.get("https://sou.zhaopin.com/login?next=search")
    assert adapter._is_login_required(driver) is True


def test_headless_login_detection_fails_with_stable_error(tmp_path: Path) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    driver = FakeDriver({search_url: login_prompt_page()})

    with pytest.raises(ZhaopinLoginRequired, match="headless"):
        list(
            adapter_with_driver(
                tmp_path,
                driver,
                headless=True,
            ).collect("Python", "上海", 1, RecordingControl())
        )


@pytest.mark.parametrize(
    "page",
    [
        verification_page(title="Security Verification"),
        verification_page(source="<div>TEOCaptchaWidget</div>"),
        verification_page(source="正在验证连接安全性"),
        verification_page(
            source=(
                "<title>Security Verification</title>"
                "<script src='https://captcha.eo.gtimg.com/TEOCaptchaWidget.js'></script>"
                "请勾选下方复选框"
            )
        ),
    ],
)
def test_headless_security_verification_detection_fails_with_stable_error(
    tmp_path: Path, page: FakeElement
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    driver = FakeDriver({search_url: page})

    with pytest.raises(ZhaopinVerificationRequired, match="headless"):
        list(
            adapter_with_driver(
                tmp_path,
                driver,
                headless=True,
            ).collect("Python", "上海", 1, RecordingControl())
        )


def test_visible_login_waits_in_same_driver_and_restores_search_page(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    listing_url = "https://jobs.zhaopin.com/jobdetail/LOGIN-RESUMED"
    normal_page = FakeElement(
        child_lists={ZhaopinSourceAdapter.LIST_ITEM_XPATH: [listing_item(listing_url)]}
    )
    driver = FakeDriver({search_url: login_prompt_page(), listing_url: detail_page()})
    events: list[tuple[str, str]] = []
    transitioned = False

    def sleeper(_seconds: float) -> None:
        nonlocal transitioned
        if not transitioned:
            transitioned = True
            driver.replace_current_page(normal_page, url=search_url)

    records = list(
        adapter_with_driver(
            tmp_path,
            driver,
            human_intervention_reporter=lambda state, url: events.append((state, url)),
            sleeper=sleeper,
        ).collect("Python", "上海", 1, RecordingControl())
    )

    assert len(records) == 1
    assert events == [("login_required", search_url), ("resumed", search_url)]
    assert driver.visited_urls == [search_url, listing_url]
    assert driver.quit_called is True


def test_visible_security_verification_waits_in_same_driver(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    listing_url = "https://jobs.zhaopin.com/jobdetail/VERIFICATION-RESUMED"
    normal_page = FakeElement(
        child_lists={ZhaopinSourceAdapter.LIST_ITEM_XPATH: [listing_item(listing_url)]}
    )
    driver = FakeDriver(
        {search_url: verification_page(source="TEOCaptchaWidget"), listing_url: detail_page()}
    )
    events: list[tuple[str, str]] = []
    transitioned = False

    def sleeper(_seconds: float) -> None:
        nonlocal transitioned
        if not transitioned:
            transitioned = True
            driver.replace_current_page(normal_page, url=search_url)

    records = list(
        adapter_with_driver(
            tmp_path,
            driver,
            human_intervention_reporter=lambda state, url: events.append((state, url)),
            sleeper=sleeper,
        ).collect("Python", "上海", 1, RecordingControl())
    )

    assert len(records) == 1
    assert events == [("verification_required", search_url), ("resumed", search_url)]
    assert driver.visited_urls == [search_url, listing_url]


def test_control_request_aborts_human_wait_and_closes_same_browser(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    driver = FakeDriver({search_url: login_prompt_page()})

    with pytest.raises(CollectionStopped):
        list(
            adapter_with_driver(tmp_path, driver).collect(
                "Python", "上海", 1, RecordingControl(stop_on_check=2)
            )
        )

    assert driver.visited_urls == [search_url]
    assert driver.quit_called is True


def test_human_intervention_timeout_is_stable_and_does_not_expose_page_data(
    tmp_path: Path,
) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    driver = FakeDriver({search_url: login_prompt_page()})
    clock_values = iter((0.0, 1.0))

    with pytest.raises(ZhaopinLoginTimeout, match="configured timeout"):
        list(
            adapter_with_driver(
                tmp_path,
                driver,
                human_intervention_timeout_seconds=0.5,
                human_intervention_poll_seconds=0.1,
                monotonic=lambda: next(clock_values),
            ).collect("Python", "上海", 1, RecordingControl())
        )

    assert driver.quit_called is True


def test_security_verification_timeout_is_stable(tmp_path: Path) -> None:
    search_url = "https://www.zhaopin.com/jobs?jl=538&pageMode=search&kw=Python"
    driver = FakeDriver({search_url: verification_page(source="TEOCaptchaWidget")})
    clock_values = iter((0.0, 1.0))

    with pytest.raises(ZhaopinVerificationTimeout, match="configured timeout"):
        list(
            adapter_with_driver(
                tmp_path,
                driver,
                human_intervention_timeout_seconds=0.5,
                human_intervention_poll_seconds=0.1,
                monotonic=lambda: next(clock_values),
            ).collect("Python", "上海", 1, RecordingControl())
        )

    assert driver.quit_called is True


def test_human_intervention_poll_interval_cannot_exceed_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        ZhaopinBrowserConfig(
            profile_directory=tmp_path / "profile",
            human_intervention_timeout_seconds=1,
            human_intervention_poll_seconds=2,
        )

import threading
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from desktop_agent.browser import BrowserTimeoutError
from desktop_agent.browser.errors import BrowserDriverUnavailableError, BrowserError
from desktop_agent.browser.playwright import (
    PlaywrightDriver,
    PlaywrightElement,
    _PlaywrightWorker,
    create_playwright_browser,
    playwright_process_id,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


class InlineWorker:
    """Runs submitted jobs immediately, matching the real worker's contract."""

    def __init__(self) -> None:
        self.stopped = False

    def submit(self, job):
        return job()

    def stop(self) -> None:
        self.stopped = True


class FakeHandle:
    def __init__(
        self,
        text: str = "",
        attributes: dict[str, str] | None = None,
        children: dict[str, "FakeHandle"] | None = None,
        children_all: dict[str, list["FakeHandle"]] | None = None,
    ) -> None:
        self.text = text
        self.attributes = attributes or {}
        self.children = children or {}
        self.children_all = children_all or {}
        self.clicked = False
        self.evaluated: list[str] = []

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    def query_selector(self, selector: str) -> "FakeHandle | None":
        return self.children.get(selector)

    def query_selector_all(self, selector: str) -> list["FakeHandle"]:
        return self.children_all.get(selector, [])

    def click(self) -> None:
        self.clicked = True

    def evaluate(self, expression: str) -> None:
        self.evaluated.append(expression)


class FakePage:
    def __init__(self, url: str = "https://www.zhaopin.com/") -> None:
        self.url = url
        self.title_text = "Zhaopin"
        self.contents = "<html></html>"
        self.body = "职位列表"
        self.selections: dict[str, FakeHandle | None] = {}
        self.multi_selections: dict[str, list[FakeHandle]] = {}
        self.goto_calls: list[tuple[str, str | None]] = []
        self.navigation_timeouts: list[float] = []
        self.goto_error: Exception | None = None

    def title(self) -> str:
        return self.title_text

    def content(self) -> str:
        return self.contents

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self.body

    def query_selector(self, selector: str) -> FakeHandle | None:
        return self.selections.get(selector)

    def query_selector_all(self, selector: str) -> list[FakeHandle]:
        return self.multi_selections.get(selector, [])

    def goto(self, url: str, *, wait_until: str | None = None) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.goto_calls.append((url, wait_until))

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self.navigation_timeouts.append(timeout)


class FakeContext:
    def __init__(self, page: FakePage | None = None) -> None:
        self.pages = [page or FakePage()]
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


PROFILE_DIRECTORY = Path("/tmp/crawler-test-profile")


def make_driver(
    page: FakePage | None = None,
    context: FakeContext | None = None,
    worker: InlineWorker | None = None,
) -> tuple[PlaywrightDriver, FakePage, FakeContext, InlineWorker]:
    resolved_page = page or FakePage()
    resolved_context = context or FakeContext(resolved_page)
    resolved_worker = worker or InlineWorker()
    driver = PlaywrightDriver(
        resolved_worker,
        resolved_context,
        resolved_page,
        profile_directory=PROFILE_DIRECTORY,
    )
    return driver, resolved_page, resolved_context, resolved_worker


def test_element_reads_text_and_attribute() -> None:
    worker = InlineWorker()
    handle = FakeHandle(text="职位名称", attributes={"href": "/job/1"})
    element = PlaywrightElement(worker, handle)

    assert element.text == "职位名称"
    assert element.get_attribute("href") == "/job/1"


def test_element_missing_child_raises_browser_error() -> None:
    element = PlaywrightElement(InlineWorker(), FakeHandle())

    with pytest.raises(BrowserError, match="not found"):
        element.find_element("xpath", "//div[@class='missing']")


def test_element_find_elements_wraps_children() -> None:
    children = [FakeHandle(text="a"), FakeHandle(text="b")]
    handle = FakeHandle(children_all={"xpath=//li": children})
    element = PlaywrightElement(InlineWorker(), handle)

    found = element.find_elements("xpath", "//li")

    assert [item.text for item in found] == ["a", "b"]


def test_element_click_translates_engine_error() -> None:
    class BrokenHandle(FakeHandle):
        def click(self) -> None:
            raise PlaywrightError("not clickable")

    element = PlaywrightElement(InlineWorker(), BrokenHandle())

    with pytest.raises(BrowserError, match="click failed"):
        element.click()


def test_driver_reads_page_state() -> None:
    driver, page, _context, _worker = make_driver()

    assert driver.current_url == page.url
    assert driver.title == "Zhaopin"
    assert driver.page_source == "<html></html>"
    assert driver.text == "职位列表"


def test_driver_navigation_uses_domcontentloaded() -> None:
    driver, page, _context, _worker = make_driver()

    driver.get("https://www.zhaopin.com/sou/jl530")

    assert page.goto_calls == [("https://www.zhaopin.com/sou/jl530", "domcontentloaded")]


def test_driver_navigation_translates_timeout() -> None:
    page = FakePage()
    page.goto_error = PlaywrightTimeoutError("timed out")
    driver, _page, _context, _worker = make_driver(page=page)

    with pytest.raises(BrowserTimeoutError, match="page load timed out"):
        driver.get("https://www.zhaopin.com/sou/jl530")


def test_driver_unsupported_script_raises() -> None:
    driver, _page, _context, _worker = make_driver()

    with pytest.raises(BrowserError, match="unsupported script"):
        driver.execute_script("window.scrollTo(0, 100)")


def test_driver_script_click_delegates_to_element_handle() -> None:
    handle = FakeHandle()
    element = PlaywrightElement(InlineWorker(), handle)
    driver, _page, _context, _worker = make_driver()

    driver.execute_script("arguments[0].click()", element)

    assert handle.evaluated == ["(node) => node.click()"]


def test_driver_page_load_timeout_is_milliseconds() -> None:
    driver, page, _context, _worker = make_driver()

    driver.set_page_load_timeout(30)

    assert page.navigation_timeouts == [30000]


def test_driver_quit_closes_context_once_and_stops_worker() -> None:
    driver, _page, context, worker = make_driver()

    driver.quit()
    driver.quit()

    assert context.closed is True
    assert worker.stopped is True


class _FakeRuntime:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakeLauncher:
    def __init__(self) -> None:
        self.runtime = _FakeRuntime()

    def start(self) -> _FakeRuntime:
        return self.runtime


def test_worker_runs_jobs_on_owner_thread() -> None:
    launcher = _FakeLauncher()
    worker = _PlaywrightWorker(runtime_factory=lambda: launcher)
    try:
        caller_thread = threading.current_thread().name
        owner_thread = worker.submit(lambda: threading.current_thread().name)
    finally:
        worker.stop()

    assert owner_thread == "playwright-owner"
    assert owner_thread != caller_thread
    assert launcher.runtime.stopped is True


def test_worker_surfaces_runtime_start_failure() -> None:
    class FailingLauncher:
        def start(self) -> None:
            raise RuntimeError("no browser runtime")

    with pytest.raises(RuntimeError, match="no browser runtime"):
        _PlaywrightWorker(runtime_factory=lambda: FailingLauncher())


class _FakeChromium:
    def __init__(self, context: FakeContext, error: Exception | None = None) -> None:
        self.context = context
        self.error = error
        self.calls: list[dict[str, object]] = []

    def launch_persistent_context(self, **kwargs: object) -> FakeContext:
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)
        return self.context


class _FakePlaywrightRuntime:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium


class _FakePlaywrightWorker:
    def __init__(self) -> None:
        self.stopped = False

    @property
    def runtime(self) -> _FakePlaywrightRuntime:
        return self._runtime

    def submit(self, job):
        return job()

    def stop(self) -> None:
        self.stopped = True


def test_create_playwright_browser_launches_persistent_context(monkeypatch) -> None:
    import desktop_agent.browser.playwright as module

    context = FakeContext()
    chromium = _FakeChromium(context)
    workers: list[_FakePlaywrightWorker] = []

    def make_worker() -> _FakePlaywrightWorker:
        worker = _FakePlaywrightWorker()
        worker._runtime = _FakePlaywrightRuntime(chromium)
        workers.append(worker)
        return worker

    monkeypatch.setattr(module, "_PlaywrightWorker", make_worker)
    config = SimpleNamespace(
        profile_directory=Path.home() / ".crawler" / "profiles" / "zhaopin",
        browser_name="chrome",
        headless=True,
        page_load_timeout_seconds=30.0,
    )

    driver = create_playwright_browser(config)

    assert isinstance(driver, PlaywrightDriver)
    assert chromium.calls[0]["user_data_dir"] == str(
        (Path.home() / ".crawler" / "profiles" / "zhaopin").expanduser().resolve()
    )
    assert chromium.calls[0]["headless"] is True
    assert chromium.calls[0]["viewport"] == {"width": 1440, "height": 1000}
    assert context.pages[0].navigation_timeouts == [30000]
    assert workers[0].stopped is False


def test_create_playwright_browser_reports_missing_browser(monkeypatch) -> None:
    import desktop_agent.browser.playwright as module

    chromium = _FakeChromium(FakeContext(), error=PlaywrightError("missing executable"))
    workers: list[_FakePlaywrightWorker] = []

    def make_worker() -> _FakePlaywrightWorker:
        worker = _FakePlaywrightWorker()
        worker._runtime = _FakePlaywrightRuntime(chromium)
        workers.append(worker)
        return worker

    monkeypatch.setattr(module, "_PlaywrightWorker", make_worker)
    monkeypatch.setattr(module, "_channel_candidates", lambda _name: (None,))
    config = SimpleNamespace(
        profile_directory=Path.home() / "profile",
        browser_name="chrome",
        headless=False,
        page_load_timeout_seconds=30.0,
    )

    with pytest.raises(BrowserDriverUnavailableError):
        create_playwright_browser(config)

    assert workers[0].stopped is True


class FakeProcess:
    def __init__(
        self,
        pid: int,
        cmdline: list[str],
        error: Exception | None = None,
    ) -> None:
        self.pid = pid
        self._cmdline = cmdline
        self._error = error

    def cmdline(self) -> list[str]:
        if self._error is not None:
            raise self._error
        return self._cmdline


def test_playwright_process_id_matches_profile_browser_process(monkeypatch) -> None:
    import desktop_agent.browser.playwright as module

    processes = [
        FakeProcess(
            100,
            ["chrome", "--type=renderer", "--user-data-dir=/tmp/crawler-test-profile"],
        ),
        FakeProcess(101, ["chrome", "--user-data-dir=/tmp/some-other-profile"]),
        FakeProcess(
            102,
            ["chrome", "--headless", "--user-data-dir=/tmp/crawler-test-profile"],
        ),
    ]
    monkeypatch.setattr(module.psutil, "process_iter", lambda: iter(processes))
    driver = SimpleNamespace(profile_directory=PROFILE_DIRECTORY)

    assert playwright_process_id(driver) == 102


def test_playwright_process_id_skips_inaccessible_processes(monkeypatch) -> None:
    import desktop_agent.browser.playwright as module

    processes = [
        FakeProcess(50, [], error=psutil.AccessDenied()),
        FakeProcess(51, ["chrome", '--user-data-dir="/tmp/crawler-test-profile"']),
    ]
    monkeypatch.setattr(module.psutil, "process_iter", lambda: iter(processes))
    driver = SimpleNamespace(profile_directory=PROFILE_DIRECTORY)

    assert playwright_process_id(driver) == 51


def test_playwright_process_id_requires_profile_directory() -> None:
    assert playwright_process_id(SimpleNamespace()) == 0

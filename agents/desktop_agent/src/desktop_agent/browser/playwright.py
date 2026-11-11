"""Playwright implementation of the browser automation contracts.

Playwright's synchronous API is bound to the thread that created its runtime;
calling it from another thread raises ``greenlet.error``. The existing shutdown
path calls ``driver.quit()`` from a dedicated thread, so every Playwright call
is marshalled onto a single owner thread through :class:`_PlaywrightWorker`.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Protocol, TypeVar, cast

import psutil
from playwright.sync_api import (
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .base import BrowserDriver, BrowserLaunchOptions, By
from .errors import BrowserDriverUnavailableError, BrowserError, BrowserTimeoutError

_T = TypeVar("_T")

_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class _RuntimeLauncher(Protocol):
    def start(self) -> Playwright: ...


class _Worker(Protocol):
    def submit(self, job: Callable[[], _T]) -> _T: ...


class _StoppableWorker(_Worker, Protocol):
    def stop(self) -> None: ...


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.strip('"')))


def _matches_profile(cmdline: Sequence[str], profile_directory: Path) -> bool:
    """True for the Chromium browser process owning ``profile_directory``.

    Child processes (renderers, GPU, ...) carry ``--type=`` and are ignored so
    the returned PID roots the whole browser process tree.
    """

    if any(argument.startswith("--type=") for argument in cmdline):
        return False
    expected = _normalized_path(str(profile_directory))
    for argument in cmdline:
        if not argument.startswith("--user-data-dir="):
            continue
        if _normalized_path(argument.split("=", 1)[1]) == expected:
            return True
    return False


def _browser_process_id(profile_directory: Path) -> int:
    for process in psutil.process_iter():
        try:
            cmdline = process.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if _matches_profile(cmdline, profile_directory):
            return process.pid
    return 0


def playwright_process_id(driver: BrowserDriver) -> int:
    """Resolve the persistent-context browser process for ``BrowserProcessSupervisor``."""

    profile_directory = getattr(driver, "profile_directory", None)
    if not isinstance(profile_directory, Path):
        return 0
    return _browser_process_id(profile_directory)


def _selector(by: str, value: str) -> str:
    if by == By.XPATH:
        return f"xpath={value}"
    if by == By.CSS_SELECTOR:
        return value
    raise BrowserError(f"unsupported selector strategy: {by}")


class _PlaywrightWorker:
    """Runs every Playwright call on one dedicated owner thread."""

    def __init__(
        self,
        *,
        runtime_factory: Callable[[], _RuntimeLauncher] = sync_playwright,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._queue: queue.Queue[tuple[Callable[[], object], Future[object]] | None] = queue.Queue()
        self._startup: Future[Playwright] = Future()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="playwright-owner",
            daemon=True,
        )
        self._thread.start()
        self._runtime = self._startup.result()

    @property
    def runtime(self) -> Playwright:
        return self._runtime

    def submit(self, job: Callable[[], _T]) -> _T:
        if self._stopped.is_set():
            raise BrowserError("Playwright runtime is no longer available")
        future: Future[object] = Future()
        self._queue.put((job, future))
        return cast(_T, future.result())

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._queue.put(None)
        self._thread.join(timeout=_WORKER_SHUTDOWN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        try:
            runtime = self._runtime_factory().start()
        except Exception as error:
            self._startup.set_exception(error)
            return
        self._startup.set_result(runtime)
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                job, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(job())
                except Exception as error:
                    future.set_exception(error)
        finally:
            with suppress(Exception):
                runtime.stop()


class PlaywrightElement:
    """Adapter over a Playwright ``ElementHandle`` translating engine errors."""

    def __init__(self, worker: _Worker, handle: ElementHandle) -> None:
        self._worker = worker
        self._handle = handle

    @property
    def text(self) -> str:
        try:
            return self._worker.submit(self._handle.inner_text)
        except PlaywrightError as error:
            raise BrowserError("element text is unavailable") from error

    def find_element(self, by: str, value: str) -> PlaywrightElement:
        selector = _selector(by, value)
        try:
            handle = self._worker.submit(lambda: self._handle.query_selector(selector))
        except PlaywrightError as error:
            raise BrowserError("element lookup failed") from error
        if handle is None:
            raise BrowserError(f"element not found: {selector}")
        return PlaywrightElement(self._worker, handle)

    def find_elements(self, by: str, value: str) -> Sequence[PlaywrightElement]:
        selector = _selector(by, value)
        try:
            handles = self._worker.submit(lambda: self._handle.query_selector_all(selector))
        except PlaywrightError as error:
            raise BrowserError("element lookup failed") from error
        return [PlaywrightElement(self._worker, handle) for handle in handles]

    def get_attribute(self, name: str) -> str | None:
        try:
            return self._worker.submit(lambda: self._handle.get_attribute(name))
        except PlaywrightError as error:
            raise BrowserError("element attribute is unavailable") from error

    def click(self) -> None:
        try:
            self._worker.submit(self._handle.click)
        except PlaywrightError as error:
            raise BrowserError("element click failed") from error


class PlaywrightDriver:
    """Adapter over a Playwright page translating engine errors."""

    def __init__(
        self,
        worker: _StoppableWorker,
        context: BrowserContext,
        page: Page,
        *,
        profile_directory: Path,
    ) -> None:
        self._worker = worker
        self._context = context
        self._page = page
        self._profile_directory = profile_directory
        self._closed = False

    @property
    def profile_directory(self) -> Path:
        """Profile root used by ``playwright_process_id`` to locate the browser."""

        return self._profile_directory

    @property
    def current_url(self) -> str:
        try:
            return self._worker.submit(lambda: self._page.url)
        except PlaywrightError as error:
            raise BrowserError("current URL is unavailable") from error

    @property
    def title(self) -> str:
        try:
            return self._worker.submit(self._page.title)
        except PlaywrightError as error:
            raise BrowserError("page title is unavailable") from error

    @property
    def page_source(self) -> str:
        try:
            return self._worker.submit(self._page.content)
        except PlaywrightError as error:
            raise BrowserError("page source is unavailable") from error

    @property
    def text(self) -> str:
        try:
            return self._worker.submit(lambda: self._page.inner_text("body"))
        except PlaywrightError as error:
            raise BrowserError("page text is unavailable") from error

    def find_element(self, by: str, value: str) -> PlaywrightElement:
        selector = _selector(by, value)
        try:
            handle = self._worker.submit(lambda: self._page.query_selector(selector))
        except PlaywrightError as error:
            raise BrowserError("element lookup failed") from error
        if handle is None:
            raise BrowserError(f"element not found: {selector}")
        return PlaywrightElement(self._worker, handle)

    def find_elements(self, by: str, value: str) -> Sequence[PlaywrightElement]:
        selector = _selector(by, value)
        try:
            handles = self._worker.submit(lambda: self._page.query_selector_all(selector))
        except PlaywrightError as error:
            raise BrowserError("element lookup failed") from error
        return [PlaywrightElement(self._worker, handle) for handle in handles]

    def get_attribute(self, name: str) -> str | None:
        # The page root exposes no attribute; the adapter never queries it.
        return None

    def click(self) -> None:
        raise BrowserError("clicking the driver root is not supported")

    def get(self, url: str) -> None:
        try:
            self._worker.submit(lambda: self._page.goto(url, wait_until="domcontentloaded"))
        except PlaywrightTimeoutError as error:
            raise BrowserTimeoutError("page load timed out") from error
        except PlaywrightError as error:
            raise BrowserError("navigation failed") from error

    def execute_script(self, script: str, *args: object) -> object:
        if (
            script == "arguments[0].click()"
            and len(args) == 1
            and isinstance(args[0], PlaywrightElement)
        ):
            element = args[0]
            try:
                self._worker.submit(lambda: element._handle.evaluate("(node) => node.click()"))
            except PlaywrightError as error:
                raise BrowserError("script execution failed") from error
            return None
        raise BrowserError(f"unsupported script: {script}")

    def implicitly_wait(self, time_to_wait: float) -> None:
        # Playwright auto-waits before acting, so implicit waits are unnecessary.
        return None

    def set_page_load_timeout(self, time_to_wait: float) -> None:
        try:
            self._worker.submit(
                lambda: self._page.set_default_navigation_timeout(time_to_wait * 1000)
            )
        except PlaywrightError as error:
            raise BrowserError("page load timeout configuration failed") from error

    def quit(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._worker.submit(self._context.close)
        finally:
            self._worker.stop()


def _channel_candidates(browser_name: str) -> tuple[str | None, ...]:
    if browser_name == "auto":
        return ("chrome", "msedge") if sys.platform == "win32" else (None,)
    if browser_name == "edge":
        return ("msedge",)
    if browser_name == "chrome" and sys.platform == "win32":
        return ("chrome",)
    return (None,)


def _launch_persistent_context(
    runtime: Playwright,
    user_data_dir: str,
    channel: str | None,
    config: BrowserLaunchOptions,
) -> BrowserContext:
    return runtime.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel=channel,
        headless=config.headless,
        viewport={"width": 1440, "height": 1000},
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )


def create_playwright_browser(config: BrowserLaunchOptions) -> BrowserDriver:
    profile_directory = config.profile_directory.expanduser().resolve()
    user_data_dir = str(profile_directory)
    last_error: Exception | None = None
    for channel in _channel_candidates(config.browser_name):
        worker = _PlaywrightWorker()
        runtime = worker.runtime
        try:
            context = worker.submit(
                partial(
                    _launch_persistent_context,
                    runtime,
                    user_data_dir,
                    channel,
                    config,
                )
            )
        except Exception as error:
            last_error = error
            worker.stop()
            continue
        page = context.pages[0] if context.pages else worker.submit(context.new_page)
        driver = PlaywrightDriver(
            worker,
            context,
            page,
            profile_directory=profile_directory,
        )
        driver.set_page_load_timeout(config.page_load_timeout_seconds)
        return driver
    raise BrowserDriverUnavailableError(
        "no Playwright Chromium or Edge browser is available"
    ) from last_error

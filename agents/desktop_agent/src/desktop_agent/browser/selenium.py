"""Selenium WebDriver implementation of the browser automation contracts.

The wrappers translate ``WebDriverException`` subclasses into the
engine-agnostic :mod:`desktop_agent.browser.errors` hierarchy so adapters stay
independent of Selenium types.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any, cast

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchDriverException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from .base import BrowserDriver, BrowserLaunchOptions, By
from .errors import BrowserDriverUnavailableError, BrowserError, BrowserTimeoutError


def _browser_candidates(browser_name: str) -> tuple[str, ...]:
    if browser_name == "auto":
        return ("chrome", "edge") if sys.platform == "win32" else ("chrome",)
    return (browser_name,)


def _configure_options(
    options: webdriver.ChromeOptions | webdriver.EdgeOptions,
    config: BrowserLaunchOptions,
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


class SeleniumElement:
    """Adapter over a Selenium WebElement translating engine errors."""

    def __init__(self, element: WebElement) -> None:
        self._element = element

    @property
    def text(self) -> str:
        try:
            return self._element.text
        except WebDriverException as error:
            raise BrowserError("element text is unavailable") from error

    def find_element(self, by: str, value: str) -> SeleniumElement:
        try:
            return SeleniumElement(self._element.find_element(by, value))
        except WebDriverException as error:
            raise BrowserError("element lookup failed") from error

    def find_elements(self, by: str, value: str) -> Sequence[SeleniumElement]:
        try:
            return [
                SeleniumElement(element)
                for element in self._element.find_elements(by, value)
            ]
        except WebDriverException as error:
            raise BrowserError("element lookup failed") from error

    def get_attribute(self, name: str) -> str | None:
        try:
            return self._element.get_attribute(name)
        except WebDriverException as error:
            raise BrowserError("element attribute is unavailable") from error

    def click(self) -> None:
        try:
            self._element.click()
        except WebDriverException as error:
            raise BrowserError("element click failed") from error


class SeleniumDriver:
    """Adapter over a Selenium WebDriver translating engine errors."""

    def __init__(self, driver: WebDriver) -> None:
        self._driver = driver

    @property
    def service(self) -> Any:
        """Expose the underlying service for ``BrowserProcessSupervisor`` PID lookup."""
        return getattr(self._driver, "service", None)

    @property
    def current_url(self) -> str:
        try:
            return self._driver.current_url
        except WebDriverException as error:
            raise BrowserError("current URL is unavailable") from error

    @property
    def title(self) -> str:
        try:
            return self._driver.title
        except WebDriverException as error:
            raise BrowserError("page title is unavailable") from error

    @property
    def page_source(self) -> str:
        try:
            return self._driver.page_source
        except WebDriverException as error:
            raise BrowserError("page source is unavailable") from error

    @property
    def text(self) -> str:
        try:
            return self._driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException as error:
            raise BrowserError("page text is unavailable") from error

    def find_element(self, by: str, value: str) -> SeleniumElement:
        try:
            return SeleniumElement(self._driver.find_element(by, value))
        except WebDriverException as error:
            raise BrowserError("element lookup failed") from error

    def find_elements(self, by: str, value: str) -> Sequence[SeleniumElement]:
        try:
            return [
                SeleniumElement(element)
                for element in self._driver.find_elements(by, value)
            ]
        except WebDriverException as error:
            raise BrowserError("element lookup failed") from error

    def get_attribute(self, name: str) -> str | None:
        # The driver root exposes no attribute; the adapter never queries it.
        return None

    def click(self) -> None:
        raise BrowserError("clicking the driver root is not supported")

    def get(self, url: str) -> None:
        try:
            self._driver.get(url)
        except TimeoutException as error:
            raise BrowserTimeoutError("page load timed out") from error
        except WebDriverException as error:
            raise BrowserError("navigation failed") from error

    def execute_script(self, script: str, *args: object) -> object:
        translated = [self._unwrap(arg) for arg in args]
        try:
            return cast(object, self._driver.execute_script(script, *translated))
        except WebDriverException as error:
            raise BrowserError("script execution failed") from error

    def implicitly_wait(self, time_to_wait: float) -> None:
        try:
            self._driver.implicitly_wait(time_to_wait)
        except WebDriverException as error:
            raise BrowserError("implicit wait failed") from error

    def set_page_load_timeout(self, time_to_wait: float) -> None:
        try:
            self._driver.set_page_load_timeout(time_to_wait)
        except WebDriverException as error:
            raise BrowserError("page load timeout configuration failed") from error

    def quit(self) -> None:
        # Left untranslated so BrowserProcessHandle observes the native error.
        self._driver.quit()

    @staticmethod
    def _unwrap(arg: object) -> object:
        return arg._element if isinstance(arg, SeleniumElement) else arg


def create_selenium_browser(config: BrowserLaunchOptions) -> BrowserDriver:
    last_error: NoSuchDriverException | None = None
    for browser_name in _browser_candidates(config.browser_name):
        try:
            if browser_name == "edge":
                edge_options = webdriver.EdgeOptions()
                _configure_options(edge_options, config)
                edge_driver = webdriver.Edge(options=edge_options)
                edge_driver.implicitly_wait(0.2)
                edge_driver.set_page_load_timeout(config.page_load_timeout_seconds)
                return SeleniumDriver(edge_driver)
            chrome_options = webdriver.ChromeOptions()
            _configure_options(chrome_options, config)
            chrome_driver = webdriver.Chrome(options=chrome_options)
            chrome_driver.implicitly_wait(0.2)
            chrome_driver.set_page_load_timeout(config.page_load_timeout_seconds)
            return SeleniumDriver(chrome_driver)
        except NoSuchDriverException as error:
            last_error = error
    raise BrowserDriverUnavailableError(
        "no configured Chrome or Edge WebDriver is available"
    ) from last_error

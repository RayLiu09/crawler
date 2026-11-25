from pathlib import Path
from types import SimpleNamespace

import pytest
from desktop_agent.browser.errors import BrowserDriverUnavailableError
from desktop_agent.browser.selenium import (
    SeleniumDriver,
    SeleniumElement,
    _set_command_timeout,
    create_selenium_browser,
)
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException


def _launch_config() -> SimpleNamespace:
    return SimpleNamespace(
        browser_name="chrome",
        profile_directory=Path("/tmp/crawler-test-profile"),
        headless=True,
        page_load_timeout_seconds=30.0,
    )


def test_set_command_timeout_bounds_driver_requests() -> None:
    client_config = SimpleNamespace(timeout=None)
    driver = SimpleNamespace(
        command_executor=SimpleNamespace(_client_config=client_config)
    )

    _set_command_timeout(driver, 45.0)  # type: ignore[arg-type]

    assert client_config.timeout == 45.0


class NullTextElement:
    """A chromedriver element whose GET_ELEMENT_TEXT value is null."""

    text = None


class NullTextDriver:
    def find_element(self, by: str, value: str) -> NullTextElement:
        return NullTextElement()


def test_element_text_normalizes_null_engine_value() -> None:
    assert SeleniumElement(NullTextElement()).text == ""


def test_driver_page_text_normalizes_null_engine_value() -> None:
    assert SeleniumDriver(NullTextDriver()).text == ""


def test_create_selenium_browser_wraps_session_start_failure(monkeypatch) -> None:
    import desktop_agent.browser.selenium as module

    def fail(*_args: object, **_kwargs: object) -> None:
        raise SessionNotCreatedException("profile is locked by another process")

    monkeypatch.setattr(module.webdriver, "Chrome", fail)

    with pytest.raises(BrowserDriverUnavailableError):
        create_selenium_browser(_launch_config())


class _StartupFailDriver:
    def __init__(self) -> None:
        self.quit_called = False
        self.command_executor = SimpleNamespace(
            _client_config=SimpleNamespace(timeout=None)
        )

    def implicitly_wait(self, value: float) -> None:
        raise WebDriverException("implicit wait failed")

    def quit(self) -> None:
        self.quit_called = True


def test_create_selenium_browser_cleans_up_partially_started_driver(
    monkeypatch,
) -> None:
    import desktop_agent.browser.selenium as module

    driver = _StartupFailDriver()
    monkeypatch.setattr(module.webdriver, "Chrome", lambda **_kwargs: driver)

    with pytest.raises(BrowserDriverUnavailableError):
        create_selenium_browser(_launch_config())

    assert driver.quit_called is True

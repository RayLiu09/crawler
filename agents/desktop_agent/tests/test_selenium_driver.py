from types import SimpleNamespace

from desktop_agent.browser.selenium import (
    SeleniumDriver,
    SeleniumElement,
    _set_command_timeout,
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

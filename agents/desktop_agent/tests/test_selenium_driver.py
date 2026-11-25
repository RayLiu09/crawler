from desktop_agent.browser.selenium import SeleniumDriver, SeleniumElement


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

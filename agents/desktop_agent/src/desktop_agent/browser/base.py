"""Engine-agnostic browser automation contracts.

Adapters depend on these protocols rather than a concrete browser engine so a
single source adapter can be collected through Selenium or Playwright
interchangeably.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class By:
    """Locator strategies as engine-agnostic string constants.

    The values match the strategies each engine already accepts, so adapters
    keep using ``By.XPATH`` without importing a specific engine.
    """

    XPATH = "xpath"
    ID = "id"
    NAME = "name"
    TAG_NAME = "tag name"
    CLASS_NAME = "class name"
    CSS_SELECTOR = "css selector"
    LINK_TEXT = "link text"
    PARTIAL_LINK_TEXT = "partial link text"


class BrowserElement(Protocol):
    @property
    def text(self) -> str: ...

    def find_element(self, by: str, value: str) -> BrowserElement: ...

    def find_elements(self, by: str, value: str) -> Sequence[BrowserElement]: ...

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


class BrowserLaunchOptions(Protocol):
    """The subset of launch settings a browser engine factory needs.

    Declared as read-only properties so any config object with matching
    attributes (for example a frozen dataclass with a narrower literal type for
    ``browser_name``) structurally satisfies the protocol.
    """

    @property
    def profile_directory(self) -> Path: ...

    @property
    def browser_name(self) -> str: ...

    @property
    def headless(self) -> bool: ...

    @property
    def page_load_timeout_seconds(self) -> float: ...

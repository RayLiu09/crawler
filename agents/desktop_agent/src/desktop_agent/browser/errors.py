"""Engine-agnostic browser automation errors.

Each browser engine implementation translates its native exceptions into this
small hierarchy so adapters never depend on Selenium or Playwright specifics.
"""


class BrowserError(RuntimeError):
    """Base error for browser automation failures."""


class BrowserTimeoutError(BrowserError):
    """A browser operation exceeded its configured timeout."""


class BrowserDriverUnavailableError(BrowserError):
    """No compatible browser or WebDriver is available."""

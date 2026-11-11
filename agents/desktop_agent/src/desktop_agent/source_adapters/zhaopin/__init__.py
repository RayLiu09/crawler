"""Zhaopin source adapter package."""

from desktop_agent.browser.selenium import create_selenium_browser

from .adapter import (
    BrowserDriverFactory,
    BrowserProfileInvalid,
    UnsupportedZhaopinRegion,
    ZhaopinAdapterError,
    ZhaopinBrowserConfig,
    ZhaopinLoginRequired,
    ZhaopinLoginTimeout,
    ZhaopinNoResults,
    ZhaopinPageLoadError,
    ZhaopinSourceAdapter,
    ZhaopinVerificationRequired,
    ZhaopinVerificationTimeout,
)

__all__ = [
    "BrowserDriverFactory",
    "BrowserProfileInvalid",
    "UnsupportedZhaopinRegion",
    "ZhaopinAdapterError",
    "ZhaopinBrowserConfig",
    "ZhaopinLoginRequired",
    "ZhaopinLoginTimeout",
    "ZhaopinNoResults",
    "ZhaopinPageLoadError",
    "ZhaopinSourceAdapter",
    "ZhaopinVerificationRequired",
    "ZhaopinVerificationTimeout",
    "create_selenium_browser",
]

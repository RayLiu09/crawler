"""Real-browser integration tests for the Playwright engine.

These launch a bundled Chromium and are skipped unless explicitly enabled:

    CRAWLER_RUN_BROWSER_TESTS=1 uv run pytest \
        agents/desktop_agent/tests/test_playwright_browser_integration.py
"""

import os
from pathlib import Path

import pytest
from desktop_agent.browser import BrowserProcessSupervisor, PsutilProcessBackend
from desktop_agent.browser.playwright import (
    create_playwright_browser,
    playwright_process_id,
)
from desktop_agent.source_adapters.zhaopin import ZhaopinBrowserConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("CRAWLER_RUN_BROWSER_TESTS") != "1",
    reason="set CRAWLER_RUN_BROWSER_TESTS=1 to run real Playwright browser tests",
)

SMOKE_PAGE = (
    "data:text/html,<html><head><title>Smoke</title></head><body>"
    "<h1>Hello</h1>"
    "<a id='link' href='/job/1'>Link</a>"
    "<button onclick=\"this.textContent='Clicked'\">Press</button>"
    "</body></html>"
)


def build_config(profile_directory: Path) -> ZhaopinBrowserConfig:
    return ZhaopinBrowserConfig(
        profile_directory=profile_directory,
        browser_name="chrome",
        headless=True,
        page_load_timeout_seconds=15,
    )


def test_real_browser_reads_and_scripts_page(tmp_path: Path) -> None:
    driver = create_playwright_browser(build_config(tmp_path / "profile"))
    try:
        driver.get(SMOKE_PAGE)

        assert driver.title == "Smoke"
        assert "Hello" in driver.text
        assert "<title>Smoke</title>" in driver.page_source

        link = driver.find_elements("xpath", "//a")[0]
        assert link.text == "Link"
        assert link.get_attribute("href") == "/job/1"

        button = driver.find_elements("xpath", "//button")[0]
        driver.execute_script("arguments[0].click()", button)
        assert driver.find_elements("xpath", "//button")[0].text == "Clicked"

        assert driver.profile_directory == (tmp_path / "profile").resolve()
        assert driver.profile_directory.is_dir()
        assert playwright_process_id(driver) > 0
    finally:
        driver.quit()


def test_supervisor_shuts_down_real_browser_process_tree(tmp_path: Path) -> None:
    driver = create_playwright_browser(build_config(tmp_path / "profile"))
    try:
        pid = playwright_process_id(driver)
        assert pid > 0
        backend = PsutilProcessBackend()
        assert backend.snapshot_tree(pid) is not None

        handle = BrowserProcessSupervisor(pid_provider=playwright_process_id).track(driver)
        handle.close()

        assert backend.snapshot_tree(pid) is None
    finally:
        driver.quit()

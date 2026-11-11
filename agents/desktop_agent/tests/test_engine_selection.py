import subprocess
import sys
from pathlib import Path

import pytest
from desktop_agent.config import AgentSettings
from pydantic import ValidationError


def write_agent_env(tmp_path: Path, *, extra: str = "") -> Path:
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "\n".join(
            [
                "CRAWLER_API_URL=https://crawler-api.test",
                "AGENT_ID=00000000-0000-4000-8000-000000000001",
                "AGENT_DEVICE_SECRET=device-secret",
                f"AGENT_DATA_DIR={tmp_path / 'agent-data'}",
                f"BROWSER_PROFILE_ROOT={tmp_path / 'profiles'}",
                extra,
            ]
        ),
        encoding="utf-8",
    )
    return env_file


def test_settings_load_browser_engine_from_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BROWSER_ENGINE", raising=False)
    env_file = write_agent_env(tmp_path, extra="BROWSER_ENGINE=playwright")

    settings = AgentSettings(_env_file=env_file)

    assert settings.browser_engine == "playwright"


def test_settings_default_browser_engine_to_selenium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BROWSER_ENGINE", raising=False)
    env_file = write_agent_env(tmp_path)

    settings = AgentSettings(_env_file=env_file)

    assert settings.browser_engine == "selenium"


def test_settings_reject_unknown_browser_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BROWSER_ENGINE", raising=False)
    env_file = write_agent_env(tmp_path, extra="BROWSER_ENGINE=firefox")

    with pytest.raises(ValidationError):
        AgentSettings(_env_file=env_file)


def test_engine_modules_are_imported_lazily(tmp_path: Path) -> None:
    script = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "from crawler_contracts.common import SourceCode",
            "from desktop_agent.runtime.supervisor import default_adapter_factory",
            "factory = default_adapter_factory",
            "factory(SourceCode.ZHAOPIN, Path('profile'), lambda *_: None, lambda *_: None)",
            "assert 'playwright' not in sys.modules, 'selenium engine imported Playwright'",
            "factory(",
            "    SourceCode.ZHAOPIN, Path('profile'), lambda *_: None, lambda *_: None,",
            "    browser_engine='playwright',",
            ")",
            "assert 'playwright' in sys.modules, 'playwright engine was not loaded'",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr

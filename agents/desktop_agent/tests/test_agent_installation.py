import asyncio
import logging
import subprocess
from pathlib import Path

import pytest
from desktop_agent.config import AgentSettings, load_agent_settings
from desktop_agent.credentials import run_credential_command
from desktop_agent.main import _configure_file_logging, _watch_shutdown_marker
from pydantic import HttpUrl, SecretStr, ValidationError

AGENT_ID = "00000000-0000-4000-8000-000000000001"


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


def base_settings(**overrides) -> dict:
    values = {
        "crawler_api_url": HttpUrl("https://crawler-api.test"),
        "agent_id": AGENT_ID,
        "agent_data_dir": Path("/tmp/agent-data"),
        "browser_profile_root": Path("/tmp/browser-profiles"),
    }
    values.update(overrides)
    return values


def test_settings_resolve_device_secret_from_keyring(monkeypatch) -> None:
    monkeypatch.setattr(
        "desktop_agent.config.keyring.get_password",
        lambda service, username: "k" * 40
        if (service, username) == ("job-collection-agent", AGENT_ID)
        else None,
    )

    settings = AgentSettings(
        **base_settings(
            agent_device_secret_keyring_service="job-collection-agent",
            agent_device_secret_keyring_username=AGENT_ID,
        )
    )

    assert settings.device_secret.get_secret_value() == "k" * 40


def test_settings_reject_ambiguous_device_credential_source() -> None:
    with pytest.raises(ValidationError, match="only one"):
        AgentSettings(
            **base_settings(
                agent_device_secret=SecretStr("d" * 40),
                agent_device_secret_keyring_service="job-collection-agent",
                agent_device_secret_keyring_username=AGENT_ID,
            )
        )


def test_absolute_config_file_is_loaded_without_exposing_secret(tmp_path: Path) -> None:
    config = tmp_path / "agent.env"
    config.write_text(
        "\n".join(
            (
                "CRAWLER_API_URL=https://crawler-api.test",
                f"AGENT_ID={AGENT_ID}",
                f"AGENT_DEVICE_SECRET={'s' * 40}",
                f"AGENT_DATA_DIR={tmp_path / 'data'}",
                f"BROWSER_PROFILE_ROOT={tmp_path / 'profiles'}",
            )
        ),
        encoding="utf-8",
    )

    settings = load_agent_settings(config)

    assert settings.agent_id.hex == AGENT_ID.replace("-", "")
    assert "s" * 40 not in repr(settings)


def test_agent_settings_validate_external_city_code_file(tmp_path: Path) -> None:
    city_codes_file = tmp_path / "city_codes.json"
    city_codes_file.write_text('{"南通": "641"}', encoding="utf-8")

    settings = AgentSettings(
        **base_settings(
            agent_device_secret=SecretStr("s" * 40),
            zhaopin_city_codes_file=city_codes_file,
        )
    )
    assert settings.zhaopin_city_codes_file == city_codes_file

    city_codes_file.write_text('{"南通": "bad"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid Zhaopin city code"):
        AgentSettings(
            **base_settings(
                agent_device_secret=SecretStr("s" * 40),
                zhaopin_city_codes_file=city_codes_file,
            )
        )


def test_credential_command_uses_hidden_reader_and_never_prints_secret(capsys) -> None:
    store = MemoryCredentialStore()
    secrets = iter(("x" * 40, "x" * 40))

    result = run_credential_command(
        ("set", "--service", "job-collection-agent", "--username", AGENT_ID),
        store=store,
        secret_reader=lambda _prompt: next(secrets),
    )

    output = capsys.readouterr()
    assert result == 0
    assert output.out.strip() == "stored"
    assert "x" * 40 not in output.out + output.err
    assert store.get_password("job-collection-agent", AGENT_ID) == "x" * 40


@pytest.mark.asyncio
async def test_shutdown_marker_requests_cooperative_stop(tmp_path: Path) -> None:
    class Supervisor:
        stopped = False

        async def stop(self) -> None:
            self.stopped = True

    supervisor = Supervisor()
    marker = tmp_path / "stop.request"
    marker.touch()

    await asyncio.wait_for(_watch_shutdown_marker(supervisor, marker), timeout=1)  # type: ignore[arg-type]

    assert supervisor.stopped is True


def test_file_logging_uses_bounded_agent_data_log(tmp_path: Path) -> None:
    agent_logger = logging.getLogger("desktop_agent")
    previous_handlers = list(agent_logger.handlers)
    try:
        log_path = _configure_file_logging(tmp_path)
        logging.getLogger("desktop_agent.test").error("runtime evidence")
        for handler in agent_logger.handlers:
            handler.flush()

        assert log_path == tmp_path / "logs" / "desktop-agent.log"
        assert "runtime evidence" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in set(agent_logger.handlers) - set(previous_handlers):
            agent_logger.removeHandler(handler)
            handler.close()


def test_linux_managed_service_templates_have_required_safety_controls() -> None:
    deploy_root = Path(__file__).parents[3] / "deploy" / "agent"
    service = (deploy_root / "linux" / "desktop-collection-agent.service.in").read_text()
    installer = deploy_root / "linux" / "install.sh"

    assert "KillMode=mixed" in service
    assert "TimeoutStopSec=180s" in service
    assert "Restart=on-failure" in service
    assert "AGENT_DEVICE_SECRET=" not in service
    subprocess.run(("bash", "-n", str(installer)), check=True)
    subprocess.run(("bash", "-n", str(deploy_root / "linux" / "uninstall.sh")), check=True)


def test_windows_task_is_interactive_and_has_cooperative_stop() -> None:
    deploy_root = Path(__file__).parents[3] / "deploy" / "agent" / "windows"
    installer = (deploy_root / "install.ps1").read_text(encoding="utf-8")
    stop_script = (deploy_root / "stop.ps1").read_text(encoding="utf-8")

    assert "-LogonType Interactive" in installer
    assert "-RestartCount 10" in installer
    assert "-MultipleInstances IgnoreNew" in installer
    assert "AGENT_DEVICE_SECRET=" not in installer
    assert "stop.request" in stop_script
    assert "Stop-ScheduledTask" in stop_script

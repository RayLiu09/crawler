import asyncio
import logging
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import FrameType
from typing import Any

from .config import get_agent_settings
from .runtime import AgentSupervisor, HttpAgentTransport


def _configure_file_logging(data_directory: Path) -> Path:
    log_directory = data_directory / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "desktop-agent.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger = logging.getLogger("desktop_agent")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    return log_path


async def _watch_shutdown_marker(
    supervisor: AgentSupervisor, marker_path: Path | None
) -> None:
    if marker_path is None:
        return
    while True:
        if marker_path.is_file():
            await supervisor.stop()
            return
        await asyncio.sleep(1)


def main() -> None:
    """Start the outbound-only desktop Agent runtime."""
    settings = get_agent_settings()
    log_path = _configure_file_logging(settings.agent_data_dir)
    logger = logging.getLogger(__name__)
    logger.info("desktop Agent starting agent_id=%s log_path=%s", settings.agent_id, log_path)

    async def run() -> None:
        async with HttpAgentTransport(
            str(settings.crawler_api_url),
            max_attempts=settings.transport_max_attempts,
            retry_base_seconds=settings.transport_retry_base_seconds,
        ) as transport:
            supervisor = AgentSupervisor(settings, transport=transport)
            loop = asyncio.get_running_loop()
            previous_handlers: dict[signal.Signals, Any] = {}

            def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
                loop.call_soon_threadsafe(asyncio.create_task, supervisor.stop())

            for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[shutdown_signal] = signal.getsignal(shutdown_signal)
                signal.signal(shutdown_signal, request_shutdown)
            try:
                marker_task = asyncio.create_task(
                    _watch_shutdown_marker(supervisor, settings.shutdown_marker_path)
                )
                try:
                    await supervisor.run()
                finally:
                    marker_task.cancel()
                    await asyncio.gather(marker_task, return_exceptions=True)
            finally:
                for shutdown_signal, previous_handler in previous_handlers.items():
                    signal.signal(shutdown_signal, previous_handler)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return
    except Exception:
        logger.exception("desktop Agent terminated unexpectedly agent_id=%s", settings.agent_id)
        raise


if __name__ == "__main__":
    main()

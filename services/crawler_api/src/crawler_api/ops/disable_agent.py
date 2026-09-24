import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from crawler_api.application.agent_credentials import AgentCredentialManager, AgentNotFound
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from crawler_api.infrastructure.database.unit_of_work import create_unit_of_work
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Disable one trusted desktop Agent.")
    parser.add_argument("--agent-id", required=True, type=UUID)
    return parser


async def disable_agent(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        manager = AgentCredentialManager(
            unit_of_work_factory=lambda: create_unit_of_work(session_factory),
            hash_device_secret=DeviceCredentialHasher(
                settings.agent_device_credential_hash_key
            ).hash,
        )
        disabled = await manager.disable(args.agent_id)
        print(
            json.dumps(
                {
                    "agentId": str(disabled.agent_id),
                    "status": "disabled",
                    "revokedSessions": disabled.revoked_sessions,
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        exit_code = asyncio.run(disable_agent(args))
    except AgentNotFound:
        print("disable rejected: agent-id was not found", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print("disable failed: database or configuration error", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

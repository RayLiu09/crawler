import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from crawler_api.application.agent_provisioning import (
    AgentAlreadyProvisioned,
    AgentProvisioner,
)
from crawler_api.config import get_settings
from crawler_api.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from crawler_api.infrastructure.database.unit_of_work import create_unit_of_work
from crawler_api.infrastructure.security.device_credentials import DeviceCredentialHasher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision one trusted desktop collection Agent.")
    parser.add_argument(
        "--display-name",
        required=True,
        help="Operator-readable name for the execution machine.",
    )
    parser.add_argument(
        "--agent-id",
        type=UUID,
        help="Stable Agent UUID; reuse it when retrying the same installation request.",
    )
    return parser


async def provision_agent(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        credential_hasher = DeviceCredentialHasher(settings.agent_device_credential_hash_key)
        provisioner = AgentProvisioner(
            unit_of_work_factory=lambda: create_unit_of_work(session_factory),
            hash_device_secret=credential_hasher.hash,
        )
        provisioned = await provisioner.provision(
            display_name=args.display_name,
            agent_id=args.agent_id,
        )
        print(
            json.dumps(
                {
                    "agentId": str(provisioned.agent_id),
                    "displayName": provisioned.display_name,
                    "deviceSecret": provisioned.device_secret,
                    "delivery": "one-time-standard-output",
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
        exit_code = asyncio.run(provision_agent(args))
    except AgentAlreadyProvisioned:
        print(
            "provisioning rejected: agent-id is already provisioned; "
            "reuse the existing one-time device secret",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception:
        print("provisioning failed: database or configuration error", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

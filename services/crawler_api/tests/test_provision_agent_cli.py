from uuid import UUID

import pytest
from crawler_api.ops.provision_agent import build_parser


def test_parser_accepts_stable_agent_id_for_install_retries() -> None:
    args = build_parser().parse_args(
        [
            "--display-name",
            "Desktop Agent A",
            "--agent-id",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ]
    )

    assert args.display_name == "Desktop Agent A"
    assert args.agent_id == UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_parser_requires_display_name() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])

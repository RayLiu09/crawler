from uuid import UUID

import pytest
from crawler_api.ops.disable_agent import build_parser as build_disable_parser
from crawler_api.ops.rotate_agent_credential import build_parser as build_rotate_parser

AGENT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


@pytest.mark.parametrize("build_parser", [build_rotate_parser, build_disable_parser])
def test_agent_operation_commands_require_agent_id(build_parser: object) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])  # type: ignore[attr-defined]


@pytest.mark.parametrize("build_parser", [build_rotate_parser, build_disable_parser])
def test_agent_operation_commands_parse_agent_id(build_parser: object) -> None:
    args = build_parser().parse_args(  # type: ignore[attr-defined]
        ["--agent-id", str(AGENT_ID)]
    )

    assert args.agent_id == AGENT_ID

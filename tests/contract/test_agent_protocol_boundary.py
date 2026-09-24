import json

from crawler_contracts.agent_protocol import (
    AgentHeartbeatRequest,
    ClaimTaskRequest,
    CompleteTaskRequest,
    ParsedRecordBatchRequest,
    TaskCheckpointRequest,
    TaskLease,
)


def test_agent_task_contracts_do_not_expose_caller_context() -> None:
    schemas = [
        AgentHeartbeatRequest.model_json_schema(),
        ClaimTaskRequest.model_json_schema(),
        TaskLease.model_json_schema(),
        TaskCheckpointRequest.model_json_schema(),
        ParsedRecordBatchRequest.model_json_schema(),
        CompleteTaskRequest.model_json_schema(),
    ]
    serialized = json.dumps(schemas).lower()

    forbidden_terms = (
        "tenant",
        "clientid",
        "client_id",
        "externalrequestid",
        "external_request_id",
        "callback",
        "metadata",
    )
    assert all(term not in serialized for term in forbidden_terms)

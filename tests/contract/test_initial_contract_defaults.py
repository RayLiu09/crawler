from uuid import uuid4

import pytest
from crawler_contracts.agent_protocol import AgentCapabilities, AgentHeartbeatRequest
from crawler_contracts.caller_auth import AccessTokenResponse
from crawler_contracts.collection_jobs import SubmitCollectionJobRequest
from crawler_contracts.common import ALL_API_SCOPES
from pydantic import ValidationError


def test_access_token_requires_explicit_inherited_scope_snapshot() -> None:
    with pytest.raises(ValidationError, match="scopes"):
        AccessTokenResponse(accessToken="t" * 32, expiresIn=3600)

    response = AccessTokenResponse(
        accessToken="t" * 32,
        expiresIn=3600,
        scopes=list(ALL_API_SCOPES),
    )

    assert response.scopes == list(ALL_API_SCOPES)
    assert response.model_dump(mode="json", by_alias=True)["scopes"] == [
        "collection:write",
        "collection:read",
        "collection:control",
    ]


def test_agent_capabilities_have_versioned_provisioning_defaults() -> None:
    capabilities = AgentCapabilities()

    assert capabilities.model_dump(mode="json", by_alias=True) == {
        "schemaVersion": 1,
        "supportedSources": [],
        "maxConcurrentTasks": 1,
        "browser": {"name": "chrome", "headful": True},
    }


def test_heartbeat_rejects_slots_above_declared_capacity() -> None:
    with pytest.raises(ValidationError, match="availableSlots cannot exceed"):
        AgentHeartbeatRequest(
            agentId=uuid4(),
            capabilities={
                "supportedSources": ["zhaopin"],
                "maxConcurrentTasks": 1,
            },
            availableSlots=2,
        )


def test_heartbeat_rejects_profile_for_undeclared_source() -> None:
    with pytest.raises(ValidationError, match="profile source must be present"):
        AgentHeartbeatRequest(
            agentId=uuid4(),
            capabilities={"supportedSources": []},
            availableSlots=1,
            profiles=[
                {
                    "source": "zhaopin",
                    "profileKey": "primary",
                    "status": "ready",
                }
            ],
        )


def test_heartbeat_rejects_duplicate_profile_keys() -> None:
    with pytest.raises(ValidationError, match="duplicate source/profileKey"):
        AgentHeartbeatRequest(
            agentId=uuid4(),
            capabilities={"supportedSources": ["zhaopin"]},
            availableSlots=1,
            profiles=[
                {
                    "source": "zhaopin",
                    "profileKey": "primary",
                    "status": "ready",
                },
                {
                    "source": "zhaopin",
                    "profileKey": "primary",
                    "status": "busy",
                },
            ],
        )


def test_collection_request_produces_exact_canonical_payload() -> None:
    request = SubmitCollectionJobRequest(
        externalRequestId="jobreq_01K5BJ8J2M0G8VN2F6ZQ4RD7Y9",
        source="zhaopin",
        queries=[{"keyword": "Python", "region": "Beijing", "pageLimit": 10}],
    )

    assert request.canonical_payload().model_dump(mode="json", by_alias=True) == {
        "schemaVersion": 1,
        "source": "zhaopin",
        "queries": [{"keyword": "Python", "region": "Beijing", "pageLimit": 10}],
        "collectionMode": "full",
        "metadata": {},
    }


def test_collection_request_allows_omitted_region_for_nationwide_search() -> None:
    request = SubmitCollectionJobRequest(
        externalRequestId="jobreq_nationwide_01",
        source="zhaopin",
        queries=[{"keyword": "Java"}],
    )

    assert request.queries[0].region == ""
    assert request.canonical_payload().model_dump(mode="json", by_alias=True)["queries"] == [
        {"keyword": "Java", "region": "", "pageLimit": 10}
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "unknown"),
        ("externalRequestId", "/absolute/path"),
        ("externalRequestId", "contains space"),
    ],
)
def test_collection_request_rejects_unknown_source_and_invalid_external_id(
    field: str, value: str
) -> None:
    payload: dict[str, object] = {
        "externalRequestId": "request-1",
        "source": "zhaopin",
        "queries": [{"keyword": "Python", "region": "Beijing"}],
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        SubmitCollectionJobRequest.model_validate(payload)

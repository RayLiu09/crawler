import hashlib
import hmac
from datetime import UTC, datetime

import pytest
from crawler_api.api.v1.caller_auth import router
from crawler_api.application.access_tokens import (
    AccessTokenIssuer,
    InvalidTenantKey,
    IssuedAccessToken,
    TenantConfigurationOutOfSync,
)
from crawler_api.application.tenant_allowlist import TenantAllowlist
from crawler_api.domain.entities import ApiAccessToken, ApiClient
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_contracts.common import ApiScope
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

TENANT_KEY = "unit-test-tenant-key"
PLAINTEXT_TOKEN = "cat_unit_test_plaintext_token_1234567890"
HASH_KEY = "unit-test-hash-key-that-is-at-least-32-bytes"
ISSUED_AT = datetime(2026, 9, 16, 8, 30, tzinfo=UTC)


def tenant_key_hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def build_allowlist(*, enabled: bool = True) -> TenantAllowlist:
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": "tenant-a",
                    "displayName": "Application A",
                    "enabled": enabled,
                    "tenantKeyHashes": [tenant_key_hash(TENANT_KEY)],
                    "scopes": ["collection:read"],
                }
            ],
        }
    )


class FakeApiClientRepository:
    def __init__(self, client: ApiClient | None) -> None:
        self.client = client
        self.updated: ApiClient | None = None

    async def get_by_tenant_id(self, tenant_id: str) -> ApiClient | None:
        if self.client is None or self.client.tenant_id != tenant_id:
            return None
        return self.client

    async def update(self, client: ApiClient) -> None:
        self.updated = client


class FakeAccessTokenRepository:
    def __init__(self) -> None:
        self.added: list[ApiAccessToken] = []

    async def add(self, token: ApiAccessToken) -> None:
        self.added.append(token)


class FakeUnitOfWork:
    def __init__(self, client: ApiClient | None) -> None:
        self.api_clients = FakeApiClientRepository(client)
        self.api_access_tokens = FakeAccessTokenRepository()
        self.entered = False
        self.committed = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def build_client(
    allowlist: TenantAllowlist,
    *,
    status: str = "active",
    config_fingerprint: str | None = None,
) -> ApiClient:
    return ApiClient(
        tenant_id="tenant-a",
        display_name="Application A",
        allowed_scopes=[ApiScope.COLLECTION_READ],
        config_fingerprint=(
            config_fingerprint or allowlist.tenants[0].config_fingerprint
        ),
        status=status,
    )


def build_issuer(
    allowlist: TenantAllowlist,
    unit_of_work: FakeUnitOfWork,
    *,
    factory_calls: list[bool] | None = None,
) -> AccessTokenIssuer:
    token_hasher = AccessTokenHasher(SecretStr(HASH_KEY))

    def unit_of_work_factory() -> FakeUnitOfWork:
        if factory_calls is not None:
            factory_calls.append(True)
        return unit_of_work

    return AccessTokenIssuer(
        allowlist=allowlist,
        unit_of_work_factory=unit_of_work_factory,  # type: ignore[arg-type]
        hash_token=token_hasher.hash,
        ttl_seconds=3600,
        clock=lambda: ISSUED_AT,
        token_generator=lambda: PLAINTEXT_TOKEN,
    )


async def test_valid_tenant_key_issues_hashed_token_with_scope_snapshot() -> None:
    allowlist = build_allowlist()
    client = build_client(allowlist)
    unit_of_work = FakeUnitOfWork(client)
    issuer = build_issuer(allowlist, unit_of_work)

    result = await issuer.issue(SecretStr(TENANT_KEY))

    assert result == IssuedAccessToken(
        access_token=PLAINTEXT_TOKEN,
        expires_in=3600,
        scopes=[ApiScope.COLLECTION_READ],
    )
    assert unit_of_work.committed
    assert len(unit_of_work.api_access_tokens.added) == 1
    stored = unit_of_work.api_access_tokens.added[0]
    expected_digest = hmac.new(
        HASH_KEY.encode(), PLAINTEXT_TOKEN.encode(), hashlib.sha256
    ).hexdigest()
    assert stored.token_hash == f"hmac-sha256:{expected_digest}"
    assert stored.token_hash != PLAINTEXT_TOKEN
    assert stored.issued_via_key_fingerprint == tenant_key_hash(TENANT_KEY)
    assert stored.scopes == client.allowed_scopes
    assert stored.scopes is not client.allowed_scopes
    assert stored.issued_at == ISSUED_AT
    assert stored.expires_at.timestamp() - stored.issued_at.timestamp() == 3600
    assert client.last_token_issued_at == ISSUED_AT
    assert unit_of_work.api_clients.updated is client


@pytest.mark.parametrize("tenant_key", [None, SecretStr("incorrect-key")])
async def test_invalid_or_missing_tenant_key_opens_no_unit_of_work(
    tenant_key: SecretStr | None,
) -> None:
    allowlist = build_allowlist()
    unit_of_work = FakeUnitOfWork(build_client(allowlist))
    factory_calls: list[bool] = []
    issuer = build_issuer(allowlist, unit_of_work, factory_calls=factory_calls)

    with pytest.raises(InvalidTenantKey):
        await issuer.issue(tenant_key)

    assert factory_calls == []


async def test_disabled_allowlist_tenant_opens_no_unit_of_work() -> None:
    allowlist = build_allowlist(enabled=False)
    unit_of_work = FakeUnitOfWork(build_client(allowlist))
    factory_calls: list[bool] = []
    issuer = build_issuer(allowlist, unit_of_work, factory_calls=factory_calls)

    with pytest.raises(InvalidTenantKey):
        await issuer.issue(SecretStr(TENANT_KEY))

    assert factory_calls == []


@pytest.mark.parametrize(
    ("client_status", "fingerprint", "missing_client"),
    [
        ("active", None, True),
        ("disabled", None, False),
        ("active", "sha256:" + "f" * 64, False),
    ],
)
async def test_database_client_must_exist_be_active_and_match_current_config(
    client_status: str,
    fingerprint: str | None,
    missing_client: bool,
) -> None:
    allowlist = build_allowlist()
    client = None if missing_client else build_client(
        allowlist,
        status=client_status,
        config_fingerprint=fingerprint,
    )
    unit_of_work = FakeUnitOfWork(client)
    issuer = build_issuer(allowlist, unit_of_work)

    with pytest.raises(TenantConfigurationOutOfSync):
        await issuer.issue(SecretStr(TENANT_KEY))

    assert unit_of_work.api_access_tokens.added == []
    assert not unit_of_work.committed


class StubIssuer:
    def __init__(self, result: IssuedAccessToken | Exception) -> None:
        self._result = result

    async def issue(self, tenant_key: SecretStr | None) -> IssuedAccessToken:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def build_route_app(result: IssuedAccessToken | Exception) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.access_token_issuer = StubIssuer(result)
    return app


async def test_token_route_uses_contract_response_and_camel_case() -> None:
    app = build_route_app(
        IssuedAccessToken(
            access_token=PLAINTEXT_TOKEN,
            expires_in=3600,
            scopes=[ApiScope.COLLECTION_READ],
        )
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/auth/token", json={"tenantKey": TENANT_KEY})

    assert response.status_code == 200
    assert response.json() == {
        "accessToken": PLAINTEXT_TOKEN,
        "tokenType": "Bearer",
        "expiresIn": 3600,
        "scopes": ["collection:read"],
    }


@pytest.mark.parametrize(
    ("payload", "failure"),
    [
        ({}, InvalidTenantKey()),
        ({"tenantKey": "invalid"}, InvalidTenantKey()),
        ({"tenantKey": TENANT_KEY}, TenantConfigurationOutOfSync()),
    ],
)
async def test_token_route_returns_uniform_401_for_authentication_failures(
    payload: dict[str, str],
    failure: Exception,
) -> None:
    app = build_route_app(failure)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/auth/token", json=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid tenant credentials"}
    assert response.headers["www-authenticate"] == "Bearer"

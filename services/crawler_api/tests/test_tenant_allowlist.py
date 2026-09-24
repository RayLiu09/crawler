import hashlib
from pathlib import Path
from uuid import UUID

import pytest
from crawler_api.application.tenant_allowlist import (
    TenantAllowlist,
    TenantAllowlistSynchronizer,
)
from crawler_api.domain.entities import ApiClient
from crawler_api.infrastructure.security.tenant_allowlist import (
    TenantAllowlistLoadError,
    load_tenant_allowlist,
)
from crawler_contracts.common import ALL_API_SCOPES, ApiScope
from pydantic import SecretStr, ValidationError


def tenant_key_hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def make_entry(
    tenant_id: str,
    key: str,
    *,
    enabled: bool = True,
    scopes: list[str] | None = None,
    callback_allowlist: list[str] | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "tenantId": tenant_id,
        "displayName": f"Application {tenant_id}",
        "enabled": enabled,
        "tenantKeyHashes": [tenant_key_hash(key)],
    }
    if scopes is not None:
        entry["scopes"] = scopes
    if callback_allowlist is not None:
        entry["callbackAllowlist"] = callback_allowlist
    return entry


def test_omitted_scopes_default_to_all_api_permissions() -> None:
    allowlist = TenantAllowlist.model_validate(
        {"version": 1, "tenants": [make_entry("tenant-a", "secret-a")]}
    )

    assert allowlist.tenants[0].scopes == list(ALL_API_SCOPES)
    key_match = allowlist.match_tenant_key(SecretStr("secret-a"))
    assert key_match is not None
    assert key_match.tenant == allowlist.tenants[0]
    assert key_match.key_fingerprint == tenant_key_hash("secret-a")
    assert allowlist.match_tenant_key(SecretStr("wrong-secret")) is None


def test_disabled_tenant_key_never_matches() -> None:
    allowlist = TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [make_entry("tenant-a", "secret-a", enabled=False)],
        }
    )

    assert allowlist.match_tenant_key(SecretStr("secret-a")) is None


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 1,
            "tenants": [
                make_entry("tenant-a", "secret-a"),
                make_entry("tenant-a", "secret-b"),
            ],
        },
        {
            "version": 1,
            "tenants": [
                make_entry("tenant-a", "shared-secret"),
                make_entry("tenant-b", "shared-secret"),
            ],
        },
        {
            "version": 1,
            "tenants": [
                make_entry(
                    "tenant-a",
                    "secret-a",
                    scopes=["collection:read", "collection:read"],
                )
            ],
        },
    ],
)
def test_duplicate_tenant_ids_key_hashes_and_scopes_are_rejected(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TenantAllowlist.model_validate(payload)


def test_config_fingerprint_is_stable_for_unordered_sets() -> None:
    first = TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    **make_entry("tenant-a", "secret-a"),
                    "tenantKeyHashes": [
                        tenant_key_hash("secret-a"),
                        tenant_key_hash("secret-b"),
                    ],
                    "scopes": ["collection:read", "collection:write"],
                    "callbackAllowlist": [
                        "https://upstream.example.com/callback/a",
                        "https://upstream.example.com/callback/b",
                    ],
                }
            ],
        }
    )
    second = TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    **make_entry("tenant-a", "secret-a"),
                    "tenantKeyHashes": [
                        tenant_key_hash("secret-b"),
                        tenant_key_hash("secret-a"),
                    ],
                    "scopes": ["collection:write", "collection:read"],
                    "callbackAllowlist": [
                        "https://upstream.example.com/callback/b",
                        "https://upstream.example.com/callback/a",
                    ],
                }
            ],
        }
    )

    assert first.tenants[0].config_fingerprint == second.tenants[0].config_fingerprint


def test_callback_allowlist_is_fingerprinted_and_exactly_matched() -> None:
    callback_url = "http://upstream.internal/api/job-collection/events"
    entry = TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                make_entry(
                    "tenant-a",
                    "secret-a",
                    callback_allowlist=[callback_url],
                )
            ],
        }
    ).tenants[0]
    without_callback = TenantAllowlist.model_validate(
        {"version": 1, "tenants": [make_entry("tenant-a", "secret-a")]}
    ).tenants[0]

    assert entry.config_fingerprint != without_callback.config_fingerprint
    assert entry.allows_callback_url(entry.callback_allowlist[0])
    different = TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                make_entry(
                    "tenant-b",
                    "secret-b",
                    callback_allowlist=[callback_url + "/other"],
                )
            ],
        }
    ).tenants[0].callback_allowlist[0]
    assert not entry.allows_callback_url(different)


@pytest.mark.parametrize(
    "callback_allowlist",
    [
        ["ftp://upstream.example.com/callback"],
        ["https://user:password@upstream.example.com/callback"],
        ["https://upstream.example.com/callback#fragment"],
        [
            "https://upstream.example.com/callback",
            "https://upstream.example.com:443/callback",
        ],
    ],
)
def test_invalid_or_duplicate_callback_allowlist_urls_are_rejected(
    callback_allowlist: list[str],
) -> None:
    with pytest.raises(ValidationError):
        TenantAllowlist.model_validate(
            {
                "version": 1,
                "tenants": [
                    make_entry(
                        "tenant-a",
                        "secret-a",
                        callback_allowlist=callback_allowlist,
                    )
                ],
            }
        )


def test_loader_rejects_duplicate_yaml_mapping_keys(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.yml"
    path.write_text("version: 1\nversion: 1\ntenants: []\n", encoding="utf-8")

    with pytest.raises(TenantAllowlistLoadError, match="missing or invalid"):
        load_tenant_allowlist(path)


def test_loader_parses_valid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.yml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "tenants:",
                "  - tenantId: tenant-a",
                "    displayName: Application A",
                "    enabled: true",
                "    tenantKeyHashes:",
                f"      - {tenant_key_hash('secret-a')}",
            ]
        ),
        encoding="utf-8",
    )

    allowlist = load_tenant_allowlist(path)

    assert allowlist.tenants[0].tenant_id == "tenant-a"
    assert allowlist.tenants[0].scopes == list(ALL_API_SCOPES)


class FakeApiClientRepository:
    def __init__(self, clients: list[ApiClient]) -> None:
        self.clients = {client.tenant_id: client for client in clients}

    async def list_all(self) -> list[ApiClient]:
        return list(self.clients.values())

    async def add(self, client: ApiClient) -> None:
        self.clients[client.tenant_id] = client

    async def update(self, client: ApiClient) -> None:
        self.clients[client.tenant_id] = client


class FakeAccessTokenRepository:
    def __init__(self) -> None:
        self.revoked_client_ids: set[UUID] = set()

    async def revoke_active_for_clients(self, client_ids: set[UUID]) -> int:
        self.revoked_client_ids.update(client_ids)
        return len(client_ids)


class FakeUnitOfWork:
    def __init__(self, clients: list[ApiClient]) -> None:
        self.api_clients = FakeApiClientRepository(clients)
        self.api_access_tokens = FakeAccessTokenRepository()
        self.committed = False

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


async def test_synchronizer_upserts_disables_and_revokes_atomically() -> None:
    tenant_a = ApiClient(
        tenant_id="tenant-a",
        display_name="Old application A",
        allowed_scopes=[ApiScope.COLLECTION_READ],
        config_fingerprint="sha256:" + "0" * 64,
    )
    stale = ApiClient(
        tenant_id="stale-tenant",
        display_name="Stale application",
        allowed_scopes=[ApiScope.COLLECTION_READ],
        config_fingerprint="sha256:" + "1" * 64,
    )
    unit_of_work = FakeUnitOfWork([tenant_a, stale])
    allowlist = TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                make_entry("tenant-a", "secret-a"),
                make_entry("tenant-b", "secret-b", enabled=False),
            ],
        }
    )
    synchronizer = TenantAllowlistSynchronizer(lambda: unit_of_work)  # type: ignore[arg-type]

    result = await synchronizer.synchronize(allowlist)

    assert result.created_clients == 1
    assert result.updated_clients == 2
    assert result.disabled_clients == 2
    assert result.revoked_tokens == 2
    assert unit_of_work.committed
    assert unit_of_work.api_clients.clients["tenant-a"].allowed_scopes == list(ALL_API_SCOPES)
    assert unit_of_work.api_clients.clients["tenant-a"].status == "active"
    assert unit_of_work.api_clients.clients["tenant-b"].status == "disabled"
    assert unit_of_work.api_clients.clients["stale-tenant"].status == "disabled"
    assert unit_of_work.api_access_tokens.revoked_client_ids == {
        unit_of_work.api_clients.clients["tenant-b"].id,
        stale.id,
    }

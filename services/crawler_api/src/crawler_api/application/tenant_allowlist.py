import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Self
from uuid import UUID, uuid4

from crawler_api.domain.entities import ApiClient
from crawler_contracts.common import ALL_API_SCOPES, ApiModel, ApiScope
from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator

from .ports import UnitOfWork

TENANT_KEY_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
TENANT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$"


class TenantAllowlistEntry(ApiModel):
    tenant_id: str = Field(min_length=1, max_length=120, pattern=TENANT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    tenant_key_hashes: list[str] = Field(min_length=1)
    scopes: list[ApiScope] = Field(default_factory=lambda: list(ALL_API_SCOPES), min_length=1)
    callback_allowlist: list[HttpUrl] = Field(default_factory=list, max_length=100)

    @field_validator("tenant_key_hashes")
    @classmethod
    def validate_tenant_key_hashes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("tenantKeyHashes contains duplicates")
        for value in values:
            if not re.fullmatch(TENANT_KEY_HASH_PATTERN, value):
                raise ValueError("tenantKeyHashes must contain lowercase sha256 fingerprints")
        return values

    @field_validator("scopes")
    @classmethod
    def validate_unique_scopes(cls, values: list[ApiScope]) -> list[ApiScope]:
        if len(values) != len(set(values)):
            raise ValueError("scopes contains duplicates")
        return values

    @field_validator("callback_allowlist")
    @classmethod
    def validate_callback_allowlist(cls, values: list[HttpUrl]) -> list[HttpUrl]:
        normalized = [str(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("callbackAllowlist contains duplicate normalized URLs")
        for value in values:
            if value.username or value.password or value.fragment:
                raise ValueError(
                    "callbackAllowlist URLs cannot contain userinfo or fragments"
                )
        return values

    @property
    def config_fingerprint(self) -> str:
        normalized = {
            "displayName": self.display_name,
            "enabled": self.enabled,
            "scopes": sorted(scope.value for scope in self.scopes),
            "tenantId": self.tenant_id,
            "tenantKeyHashes": sorted(self.tenant_key_hashes),
            "callbackAllowlist": sorted(str(url) for url in self.callback_allowlist),
        }
        encoded = json.dumps(
            normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def matches_tenant_key(self, tenant_key: SecretStr) -> bool:
        candidate = "sha256:" + hashlib.sha256(tenant_key.get_secret_value().encode()).hexdigest()
        matched = False
        for configured_hash in self.tenant_key_hashes:
            matched = hmac.compare_digest(candidate, configured_hash) or matched
        return self.enabled and matched

    def allows_callback_url(self, url: HttpUrl) -> bool:
        candidate = str(url)
        return self.enabled and any(
            hmac.compare_digest(candidate, str(allowed_url))
            for allowed_url in self.callback_allowlist
        )


class TenantAllowlist(ApiModel):
    version: Literal[1]
    tenants: list[TenantAllowlistEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_global_uniqueness(self) -> Self:
        tenant_ids = [tenant.tenant_id for tenant in self.tenants]
        if len(tenant_ids) != len(set(tenant_ids)):
            raise ValueError("tenantId must be globally unique")

        key_hashes = [key_hash for tenant in self.tenants for key_hash in tenant.tenant_key_hashes]
        if len(key_hashes) != len(set(key_hashes)):
            raise ValueError("tenant key fingerprints must be globally unique")
        return self

    def match_tenant_key(self, tenant_key: SecretStr) -> "TenantKeyMatch | None":
        candidate = "sha256:" + hashlib.sha256(tenant_key.get_secret_value().encode()).hexdigest()
        matched_entry: TenantAllowlistEntry | None = None
        matched_fingerprint: str | None = None
        for tenant in self.tenants:
            for configured_hash in tenant.tenant_key_hashes:
                if hmac.compare_digest(candidate, configured_hash) and tenant.enabled:
                    matched_entry = tenant
                    matched_fingerprint = configured_hash
        if matched_entry is None or matched_fingerprint is None:
            return None
        return TenantKeyMatch(
            tenant=matched_entry,
            key_fingerprint=matched_fingerprint,
        )

    def get_enabled_tenant(self, tenant_id: str) -> TenantAllowlistEntry | None:
        return next(
            (
                tenant
                for tenant in self.tenants
                if tenant.tenant_id == tenant_id and tenant.enabled
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class TenantKeyMatch:
    tenant: TenantAllowlistEntry
    key_fingerprint: str


@dataclass(frozen=True, slots=True)
class TenantAllowlistSyncResult:
    created_clients: int
    updated_clients: int
    disabled_clients: int
    revoked_tokens: int


class TenantAllowlistSynchronizer:
    def __init__(self, unit_of_work_factory: Callable[[], UnitOfWork]) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def synchronize(self, allowlist: TenantAllowlist) -> TenantAllowlistSyncResult:
        created = 0
        updated = 0
        disabled_client_ids: set[UUID] = set()

        async with self._unit_of_work_factory() as unit_of_work:
            existing_clients = {
                client.tenant_id: client for client in await unit_of_work.api_clients.list_all()
            }
            configured_tenant_ids = {tenant.tenant_id for tenant in allowlist.tenants}

            for tenant in allowlist.tenants:
                status = "active" if tenant.enabled else "disabled"
                existing = existing_clients.get(tenant.tenant_id)
                synchronized = ApiClient(
                    id=existing.id if existing is not None else uuid4(),
                    tenant_id=tenant.tenant_id,
                    display_name=tenant.display_name,
                    allowed_scopes=list(tenant.scopes),
                    config_fingerprint=tenant.config_fingerprint,
                    status=status,
                    last_token_issued_at=(
                        existing.last_token_issued_at if existing is not None else None
                    ),
                    created_at=existing.created_at if existing is not None else None,
                    updated_at=existing.updated_at if existing is not None else None,
                )
                if existing is None:
                    await unit_of_work.api_clients.add(synchronized)
                    created += 1
                elif synchronized != existing:
                    await unit_of_work.api_clients.update(synchronized)
                    updated += 1
                if not tenant.enabled:
                    disabled_client_ids.add(synchronized.id)

            for tenant_id, existing in existing_clients.items():
                if tenant_id in configured_tenant_ids:
                    continue
                if existing.status != "disabled":
                    existing.status = "disabled"
                    await unit_of_work.api_clients.update(existing)
                    updated += 1
                disabled_client_ids.add(existing.id)

            revoked = await unit_of_work.api_access_tokens.revoke_active_for_clients(
                disabled_client_ids
            )
            await unit_of_work.commit()

        return TenantAllowlistSyncResult(
            created_clients=created,
            updated_clients=updated,
            disabled_clients=len(disabled_client_ids),
            revoked_tokens=revoked,
        )

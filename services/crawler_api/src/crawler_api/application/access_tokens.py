import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from crawler_api.domain.entities import ApiAccessToken
from crawler_contracts.common import ApiScope
from pydantic import SecretStr

from .ports import UnitOfWork
from .tenant_allowlist import TenantAllowlist


class AccessTokenIssuanceDenied(Exception):
    """Base error for authentication failures that must be indistinguishable over HTTP."""


class InvalidTenantKey(AccessTokenIssuanceDenied):
    pass


class TenantConfigurationOutOfSync(AccessTokenIssuanceDenied):
    pass


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    access_token: str
    expires_in: int
    scopes: list[ApiScope]


def generate_access_token() -> str:
    return f"cat_{secrets.token_urlsafe(32)}"


class AccessTokenIssuer:
    def __init__(
        self,
        *,
        allowlist: TenantAllowlist,
        unit_of_work_factory: Callable[[], UnitOfWork],
        hash_token: Callable[[str], str],
        ttl_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_generator: Callable[[], str] = generate_access_token,
    ) -> None:
        self._allowlist = allowlist
        self._unit_of_work_factory = unit_of_work_factory
        self._hash_token = hash_token
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._token_generator = token_generator

    async def issue(self, tenant_key: SecretStr | None) -> IssuedAccessToken:
        if tenant_key is None:
            raise InvalidTenantKey

        key_match = self._allowlist.match_tenant_key(tenant_key)
        if key_match is None:
            raise InvalidTenantKey

        async with self._unit_of_work_factory() as unit_of_work:
            client = await unit_of_work.api_clients.get_by_tenant_id(key_match.tenant.tenant_id)
            if (
                client is None
                or client.status != "active"
                or client.config_fingerprint != key_match.tenant.config_fingerprint
            ):
                raise TenantConfigurationOutOfSync

            issued_at = self._clock()
            expires_at = issued_at + timedelta(seconds=self._ttl_seconds)
            plaintext_token = self._token_generator()
            scopes = list(client.allowed_scopes)
            token = ApiAccessToken(
                client_id=client.id,
                token_hash=self._hash_token(plaintext_token),
                issued_via_key_fingerprint=key_match.key_fingerprint,
                scopes=scopes,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            await unit_of_work.api_access_tokens.add(token)
            client.last_token_issued_at = issued_at
            await unit_of_work.api_clients.update(client)
            await unit_of_work.commit()

        return IssuedAccessToken(
            access_token=plaintext_token,
            expires_in=self._ttl_seconds,
            scopes=scopes,
        )

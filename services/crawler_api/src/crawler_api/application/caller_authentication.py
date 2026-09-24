from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from crawler_api.domain.entities import ApiAccessToken, ApiClient
from crawler_contracts.common import ApiScope
from pydantic import SecretStr

from .ports import UnitOfWork


class CallerAccessTokenAuthenticationDenied(Exception):
    """Uniform failure for invalid, expired, revoked, or disabled caller Tokens."""


class CallerAccessTokenInsufficientScope(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    client: ApiClient
    token: ApiAccessToken


class CallerAccessTokenAuthenticator:
    def __init__(self, hash_access_token: Callable[[str], str]) -> None:
        self._hash_access_token = hash_access_token

    async def authenticate(
        self,
        *,
        unit_of_work: UnitOfWork,
        access_token: SecretStr,
        required_scope: ApiScope,
        authenticated_at: datetime,
    ) -> AuthenticatedCaller:
        token_hash = self._hash_access_token(access_token.get_secret_value())
        initial_token = await unit_of_work.api_access_tokens.get_by_token_hash(token_hash)
        if initial_token is None:
            raise CallerAccessTokenAuthenticationDenied

        client = await unit_of_work.api_clients.get_for_share(initial_token.client_id)
        token = await unit_of_work.api_access_tokens.get(initial_token.id)
        if (
            client is None
            or token is None
            or token.client_id != client.id
            or client.status != "active"
            or token.revoked_at is not None
            or token.expires_at <= authenticated_at
        ):
            raise CallerAccessTokenAuthenticationDenied
        if required_scope not in token.scopes:
            raise CallerAccessTokenInsufficientScope

        await unit_of_work.api_access_tokens.update_last_used(token.id, authenticated_at)
        token.last_used_at = authenticated_at
        return AuthenticatedCaller(client=client, token=token)

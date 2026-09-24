from typing import Literal

from pydantic import Field, SecretStr

from .common import ApiModel, ApiScope


class IssueAccessTokenRequest(ApiModel):
    # An omitted credential is an authentication failure, not a request-shape disclosure.
    tenant_key: SecretStr | None = None


class AccessTokenResponse(ApiModel):
    # This response-only secret must be returned exactly once to the caller.
    access_token: str = Field(min_length=32)
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int = Field(gt=0)
    # Required so token issuance must copy the owning client's allowed_scopes snapshot.
    scopes: list[ApiScope]


class RevokeAccessTokenRequest(ApiModel):
    token_id: str | None = Field(default=None, min_length=1, max_length=100)

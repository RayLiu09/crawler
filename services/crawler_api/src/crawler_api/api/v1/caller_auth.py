from typing import cast

from crawler_contracts.caller_auth import AccessTokenResponse, IssueAccessTokenRequest
from fastapi import APIRouter, HTTPException, Request, status

from crawler_api.application.access_tokens import (
    AccessTokenIssuanceDenied,
    AccessTokenIssuer,
)

router = APIRouter(prefix="/v1/auth", tags=["caller-auth"])


@router.post(
    "/token",
    response_model=AccessTokenResponse,
    status_code=status.HTTP_200_OK,
)
async def issue_access_token(
    payload: IssueAccessTokenRequest,
    request: Request,
) -> AccessTokenResponse:
    issuer = cast(AccessTokenIssuer, request.app.state.access_token_issuer)
    try:
        issued = await issuer.issue(payload.tenant_key)
    except AccessTokenIssuanceDenied as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid tenant credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    return AccessTokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        scopes=issued.scopes,
    )

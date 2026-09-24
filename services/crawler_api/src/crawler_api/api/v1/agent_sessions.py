from typing import cast

from crawler_contracts.agent_protocol import AgentSessionRequest, AgentSessionResponse
from fastapi import APIRouter, HTTPException, Request, status

from crawler_api.application.agent_sessions import (
    AgentAuthenticationDenied,
    AgentSessionIssuer,
)

router = APIRouter(prefix="/v1/agent", tags=["agent-auth"])


@router.post(
    "/sessions",
    response_model=AgentSessionResponse,
    status_code=status.HTTP_200_OK,
)
async def issue_agent_session(
    payload: AgentSessionRequest,
    request: Request,
) -> AgentSessionResponse:
    issuer = cast(AgentSessionIssuer, request.app.state.agent_session_issuer)
    try:
        issued = await issuer.issue(payload.agent_id, payload.device_secret)
    except AgentAuthenticationDenied as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid agent credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    return AgentSessionResponse(
        session_token=issued.session_token,
        expires_at=issued.expires_at,
    )

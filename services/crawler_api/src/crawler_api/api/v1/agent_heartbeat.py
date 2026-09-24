from typing import Annotated, cast

from crawler_contracts.agent_protocol import AgentHeartbeatRequest
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from crawler_api.application.agent_authentication import (
    AgentSessionAuthenticationDenied,
)
from crawler_api.application.agent_heartbeats import AgentHeartbeatRecorder

router = APIRouter(prefix="/v1/agent", tags=["agent-runtime"])
agent_bearer = HTTPBearer(auto_error=False)


def authentication_failure() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid agent session",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post(
    "/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def record_agent_heartbeat(
    payload: AgentHeartbeatRequest,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(agent_bearer),
    ],
) -> Response:
    if credentials is None:
        raise authentication_failure()

    recorder = cast(AgentHeartbeatRecorder, request.app.state.agent_heartbeat_recorder)
    try:
        await recorder.record(
            session_token=SecretStr(credentials.credentials),
            heartbeat=payload,
        )
    except AgentSessionAuthenticationDenied as error:
        raise authentication_failure() from error

    return Response(status_code=status.HTTP_204_NO_CONTENT)

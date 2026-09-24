from collections.abc import Awaitable
from typing import Annotated, cast
from uuid import UUID

from crawler_contracts.agent_protocol import (
    ClaimTaskRequest,
    CompleteTaskRequest,
    LeaseReference,
    ParsedRecordBatchRequest,
    ReleaseTaskRequest,
    TaskCheckpointRequest,
    TaskControlResponse,
    TaskLease,
)
from crawler_contracts.parsed_jobs import ParsedRecordBatchResponse
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from crawler_api.application.agent_authentication import (
    AgentSessionAuthenticationDenied,
)
from crawler_api.application.agent_task_claims import (
    AgentTaskClaimer,
    AgentTaskClaimUnavailable,
    ClaimRequestConflict,
)
from crawler_api.application.agent_task_runtime import (
    AgentTaskRuntime,
    TaskRuntimeStateConflict,
)
from crawler_api.application.parsed_records import (
    ParsedRecordBatchConflict,
    ParsedRecordBatchIngestor,
    ParsedRecordTaskMismatch,
)
from crawler_api.application.task_leases import TaskLeaseStateConflict

router = APIRouter(prefix="/v1/agent/tasks", tags=["agent-runtime"])
agent_bearer = HTTPBearer(auto_error=False)


def authentication_failure() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid agent session",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post(
    "/claim",
    response_model=TaskLease,
    responses={status.HTTP_204_NO_CONTENT: {"description": "No matching task"}},
)
async def claim_agent_task(
    payload: ClaimTaskRequest,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(agent_bearer),
    ],
) -> TaskLease | Response:
    if credentials is None:
        raise authentication_failure()

    claimer = cast(AgentTaskClaimer, request.app.state.agent_task_claimer)
    try:
        lease = await claimer.claim(
            session_token=SecretStr(credentials.credentials),
            request=payload,
        )
    except AgentSessionAuthenticationDenied as error:
        raise authentication_failure() from error
    except AgentTaskClaimUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="agent is not ready to claim a task",
        ) from error
    except ClaimRequestConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="claim request cannot be replayed",
        ) from error

    if lease is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return lease


def lease_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="task lease is not active",
    )


def runtime_service(request: Request) -> AgentTaskRuntime:
    return cast(AgentTaskRuntime, request.app.state.agent_task_runtime)


def session_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> SecretStr:
    if credentials is None:
        raise authentication_failure()
    return SecretStr(credentials.credentials)


async def run_lease_operation[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except AgentSessionAuthenticationDenied as error:
        raise authentication_failure() from error
    except (TaskLeaseStateConflict, TaskRuntimeStateConflict) as error:
        raise lease_conflict() from error


@router.post("/{task_id}/heartbeat", response_model=TaskControlResponse)
async def heartbeat_task_lease(
    task_id: UUID,
    payload: LeaseReference,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(agent_bearer),
    ],
) -> TaskControlResponse:
    return await run_lease_operation(
        runtime_service(request).heartbeat(
            session_token=session_token(credentials),
            task_id=task_id,
            request=payload,
        )
    )


@router.post("/{task_id}/checkpoint", response_model=TaskControlResponse)
async def checkpoint_task_lease(
    task_id: UUID,
    payload: TaskCheckpointRequest,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(agent_bearer),
    ],
) -> TaskControlResponse:
    return await run_lease_operation(
        runtime_service(request).checkpoint(
            session_token=session_token(credentials),
            task_id=task_id,
            request=payload,
        )
    )


@router.post("/{task_id}/complete", status_code=status.HTTP_204_NO_CONTENT)
async def complete_task_lease(
    task_id: UUID,
    payload: CompleteTaskRequest,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(agent_bearer),
    ],
) -> Response:
    await run_lease_operation(
        runtime_service(request).complete(
            session_token=session_token(credentials),
            task_id=task_id,
            request=payload,
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_task_lease(
    task_id: UUID,
    payload: ReleaseTaskRequest,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(agent_bearer),
    ],
) -> Response:
    await run_lease_operation(
        runtime_service(request).release(
            session_token=session_token(credentials),
            task_id=task_id,
            request=payload,
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/records", response_model=ParsedRecordBatchResponse)
async def upload_parsed_records(
    task_id: UUID,
    payload: ParsedRecordBatchRequest,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(agent_bearer),
    ],
) -> ParsedRecordBatchResponse:
    ingestor = cast(
        ParsedRecordBatchIngestor, request.app.state.parsed_record_batch_ingestor
    )
    try:
        return await ingestor.ingest(
            session_token=session_token(credentials),
            task_id=task_id,
            request=payload,
        )
    except AgentSessionAuthenticationDenied as error:
        raise authentication_failure() from error
    except TaskLeaseStateConflict as error:
        raise lease_conflict() from error
    except (ParsedRecordBatchConflict, ParsedRecordTaskMismatch) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="parsed record batch conflicts with the active task",
        ) from error

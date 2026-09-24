from typing import Annotated, cast
from uuid import UUID

from crawler_contracts.collection_jobs import (
    CollectionJobResponse,
    CollectionTaskStatusCounts,
    RotateCollectionJobCallbackTokenRequest,
    RotateCollectionJobCallbackTokenResponse,
    SubmitCollectionJobRequest,
    SubmitCollectionJobResponse,
)
from crawler_contracts.events import CollectionEventPageResponse
from crawler_contracts.parsed_jobs import ParsedJobRecordPageResponse
from crawler_contracts.webhooks import (
    ReplayWebhookDeliveryResponse,
    WebhookDeliveryPageResponse,
)
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from crawler_api.application.callback_token_rotation import (
    CallbackTokenRotationConflict,
    CallbackTokenRotationInvalid,
    CallbackTokenRotationNotFound,
    CollectionJobCallbackTokenRotator,
)
from crawler_api.application.caller_authentication import (
    CallerAccessTokenAuthenticationDenied,
    CallerAccessTokenInsufficientScope,
)
from crawler_api.application.collection_events import (
    CollectionEventCursorInvalid,
    CollectionEventJobNotFound,
    CollectionEventReader,
)
from crawler_api.application.collection_jobs import (
    CollectionJobCallbackInvalid,
    CollectionJobControlAction,
    CollectionJobControlConflict,
    CollectionJobController,
    CollectionJobDetails,
    CollectionJobIdempotencyConflict,
    CollectionJobNotFound,
    CollectionJobReader,
    CollectionJobSubmitter,
    InvalidIdempotencyKey,
)
from crawler_api.application.parsed_records import (
    ParsedRecordCursorInvalid,
    ParsedRecordReader,
)
from crawler_api.application.webhook_operations import (
    WebhookDeliveryCursorInvalid,
    WebhookDeliveryNotFound,
    WebhookDeliveryOperator,
    WebhookDeliveryReplayConflict,
)
from crawler_api.domain.enums import CollectionState
from crawler_api.domain.webhooks import WebhookOutboxStatus

router = APIRouter(prefix="/v1/collection-jobs", tags=["collection-jobs"])
caller_bearer = HTTPBearer(auto_error=False)


def authentication_failure() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def insufficient_scope(scope: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="insufficient access token scope",
        headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
    )


def collection_job_response(details: CollectionJobDetails) -> CollectionJobResponse:
    job = details.job
    if job.created_at is None or job.updated_at is None:
        raise RuntimeError("persisted collection job is missing timestamps")
    counts = details.task_status_counts
    return CollectionJobResponse(
        collection_job_id=job.id,
        external_request_id=job.external_request_id,
        source=job.request_payload.source,
        desired_state=job.desired_state.value,
        status=job.status.value,
        version=job.version,
        task_status_counts=CollectionTaskStatusCounts(
            total=sum(counts.values()),
            queued=counts.get(CollectionState.QUEUED, 0),
            running=counts.get(CollectionState.RUNNING, 0),
            pausing=counts.get(CollectionState.PAUSING, 0),
            paused=counts.get(CollectionState.PAUSED, 0),
            awaiting_human=counts.get(CollectionState.AWAITING_HUMAN, 0),
            cancelling=counts.get(CollectionState.CANCELLING, 0),
            cancelled=counts.get(CollectionState.CANCELLED, 0),
            succeeded=counts.get(CollectionState.SUCCEEDED, 0),
            partially_succeeded=counts.get(CollectionState.PARTIALLY_SUCCEEDED, 0),
            failed=counts.get(CollectionState.FAILED, 0),
        ),
        processed_count=details.processed_count,
        failure_code_counts=details.failure_code_counts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )


@router.post(
    "",
    response_model=SubmitCollectionJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_collection_job(
    payload: SubmitCollectionJobRequest,
    request: Request,
    response: Response,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=200),
    ] = None,
) -> SubmitCollectionJobResponse:
    if credentials is None:
        raise authentication_failure()
    if idempotency_key is None or not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    submitter = cast(CollectionJobSubmitter, request.app.state.collection_job_submitter)
    try:
        submitted = await submitter.submit(
            access_token=SecretStr(credentials.credentials),
            idempotency_key=SecretStr(idempotency_key),
            request=payload,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:write") from error
    except InvalidIdempotencyKey as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid Idempotency-Key header",
        ) from error
    except CollectionJobIdempotencyConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency identifiers conflict with an existing collection job",
        ) from error
    except CollectionJobCallbackInvalid as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="callback configuration is invalid or not allowed",
        ) from error

    response.headers["Location"] = f"/v1/collection-jobs/{submitted.job.id}"
    return SubmitCollectionJobResponse(
        collection_job_id=submitted.job.id,
        external_request_id=submitted.job.external_request_id,
        callback_token_expires_at=submitted.callback_token_expires_at,
    )


@router.get("/{job_id}", response_model=CollectionJobResponse)
async def get_collection_job(
    job_id: UUID,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
) -> CollectionJobResponse:
    if credentials is None:
        raise authentication_failure()

    reader = cast(CollectionJobReader, request.app.state.collection_job_reader)
    try:
        details = await reader.get(
            access_token=SecretStr(credentials.credentials),
            job_id=job_id,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:read") from error
    except CollectionJobNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="collection job not found",
        ) from error
    return collection_job_response(details)


@router.get("/{job_id}/records", response_model=ParsedJobRecordPageResponse)
async def list_collection_job_records(
    job_id: UUID,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ParsedJobRecordPageResponse:
    if credentials is None:
        raise authentication_failure()
    reader = cast(ParsedRecordReader, request.app.state.parsed_record_reader)
    try:
        return await reader.list_for_job(
            access_token=SecretStr(credentials.credentials),
            job_id=job_id,
            cursor=cursor,
            limit=limit,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:read") from error
    except CollectionJobNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="collection job not found",
        ) from error
    except ParsedRecordCursorInvalid as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid records cursor",
        ) from error


@router.get("/{job_id}/events", response_model=CollectionEventPageResponse)
async def list_collection_job_events(
    job_id: UUID,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CollectionEventPageResponse:
    if credentials is None:
        raise authentication_failure()
    reader = cast(CollectionEventReader, request.app.state.collection_event_reader)
    try:
        return await reader.list_for_job(
            access_token=SecretStr(credentials.credentials),
            job_id=job_id,
            cursor=cursor,
            limit=limit,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:read") from error
    except CollectionEventJobNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="collection job not found",
        ) from error
    except CollectionEventCursorInvalid as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid events cursor",
        ) from error


@router.put(
    "/{job_id}/callback-token",
    response_model=RotateCollectionJobCallbackTokenResponse,
)
async def rotate_collection_job_callback_token(
    job_id: UUID,
    payload: RotateCollectionJobCallbackTokenRequest,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=200),
    ] = None,
) -> RotateCollectionJobCallbackTokenResponse:
    if credentials is None:
        raise authentication_failure()
    if idempotency_key is None or not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    rotator = cast(
        CollectionJobCallbackTokenRotator,
        request.app.state.collection_job_callback_token_rotator,
    )
    try:
        result = await rotator.rotate(
            access_token=SecretStr(credentials.credentials),
            idempotency_key=SecretStr(idempotency_key),
            job_id=job_id,
            request=payload,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:control") from error
    except InvalidIdempotencyKey as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid Idempotency-Key header",
        ) from error
    except CallbackTokenRotationNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="collection job callback not found",
        ) from error
    except CallbackTokenRotationConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="callback Token rotation conflicts with existing state",
        ) from error
    except CallbackTokenRotationInvalid as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="callback Token rotation request is invalid",
        ) from error
    return RotateCollectionJobCallbackTokenResponse(
        callback_token_expires_at=result.callback_token_expires_at,
        credential_version=result.credential_version,
    )


@router.get(
    "/{job_id}/webhook-deliveries",
    response_model=WebhookDeliveryPageResponse,
)
async def list_webhook_deliveries(
    job_id: UUID,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
    delivery_status: Annotated[
        WebhookOutboxStatus,
        Query(alias="status"),
    ] = WebhookOutboxStatus.PERMANENTLY_FAILED,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WebhookDeliveryPageResponse:
    if credentials is None:
        raise authentication_failure()
    operator = cast(WebhookDeliveryOperator, request.app.state.webhook_delivery_operator)
    try:
        return await operator.list_for_job(
            access_token=SecretStr(credentials.credentials),
            job_id=job_id,
            status=delivery_status,
            cursor=cursor,
            limit=limit,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:read") from error
    except WebhookDeliveryNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="collection job not found",
        ) from error
    except WebhookDeliveryCursorInvalid as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid webhook deliveries cursor",
        ) from error


@router.post(
    "/{job_id}/webhook-deliveries/{outbox_id}/replay",
    response_model=ReplayWebhookDeliveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def replay_webhook_delivery(
    job_id: UUID,
    outbox_id: UUID,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", max_length=200),
    ] = None,
) -> ReplayWebhookDeliveryResponse:
    if credentials is None:
        raise authentication_failure()
    if idempotency_key is None or not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    operator = cast(WebhookDeliveryOperator, request.app.state.webhook_delivery_operator)
    try:
        return await operator.replay(
            access_token=SecretStr(credentials.credentials),
            idempotency_key=SecretStr(idempotency_key),
            job_id=job_id,
            outbox_id=outbox_id,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:control") from error
    except InvalidIdempotencyKey as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid Idempotency-Key header",
        ) from error
    except WebhookDeliveryNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="webhook delivery not found",
        ) from error
    except WebhookDeliveryReplayConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="webhook delivery is not eligible for replay",
        ) from error


@router.post("/{job_id}/{action}", response_model=CollectionJobResponse)
async def control_collection_job(
    job_id: UUID,
    action: CollectionJobControlAction,
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(caller_bearer),
    ],
) -> CollectionJobResponse:
    if credentials is None:
        raise authentication_failure()

    controller = cast(CollectionJobController, request.app.state.collection_job_controller)
    try:
        details = await controller.control(
            access_token=SecretStr(credentials.credentials),
            job_id=job_id,
            action=action,
        )
    except CallerAccessTokenAuthenticationDenied as error:
        raise authentication_failure() from error
    except CallerAccessTokenInsufficientScope as error:
        raise insufficient_scope("collection:control") from error
    except CollectionJobNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="collection job not found",
        ) from error
    except CollectionJobControlConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="collection job does not allow the requested control transition",
        ) from error
    return collection_job_response(details)

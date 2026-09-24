import base64
import binascii
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from crawler_api.domain.entities import CollectionJob, CollectionTask
from crawler_api.domain.enums import TERMINAL_COLLECTION_STATES
from crawler_api.domain.events import StoredCollectionEvent
from crawler_api.domain.webhooks import WebhookOutboxEntry
from crawler_contracts.common import ApiScope
from crawler_contracts.events import (
    CollectionEvent,
    CollectionEventPageResponse,
    CollectionEventType,
    CollectionJobAcceptedEventPayload,
    CollectionJobDesiredStateChangedEventPayload,
    CollectionJobTerminalEventPayload,
)
from pydantic import SecretStr

from .caller_authentication import CallerAccessTokenAuthenticator
from .ports import UnitOfWork


class CollectionEventCursorInvalid(Exception):
    pass


class CollectionEventJobNotFound(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CollectionEventCursor:
    job_id: UUID
    occurred_at: datetime
    event_id: UUID


async def add_event_with_outbox(
    unit_of_work: UnitOfWork,
    event: StoredCollectionEvent,
    *,
    callback_config_id: UUID | None = None,
) -> None:
    if callback_config_id is None:
        callback_config = await unit_of_work.callback_configs.get_for_job(
            event.collection_job_id
        )
        callback_config_id = (
            callback_config.id if callback_config is not None else None
        )
    await unit_of_work.collection_events.add(event)
    if callback_config_id is not None:
        await unit_of_work.webhook_outbox.add(
            WebhookOutboxEntry(
                event_id=event.id,
                callback_config_id=callback_config_id,
                next_attempt_at=event.occurred_at,
            )
        )


class CollectionEventReader:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        authenticator: CallerAccessTokenAuthenticator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authenticator = authenticator
        self._clock = clock

    async def list_for_job(
        self,
        *,
        access_token: SecretStr,
        job_id: UUID,
        cursor: str | None,
        limit: int,
    ) -> CollectionEventPageResponse:
        read_at = self._clock()
        position = decode_event_cursor(cursor, job_id) if cursor else None
        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_READ,
                authenticated_at=read_at,
            )
            job = await unit_of_work.collection_jobs.get_for_client(
                job_id, caller.client.id
            )
            if job is None:
                raise CollectionEventJobNotFound
            events = await unit_of_work.collection_events.list_for_job(
                job.id,
                after_occurred_at=position.occurred_at if position else None,
                after_id=position.event_id if position else None,
                limit=limit + 1,
            )
            has_more = len(events) > limit
            page_events = events[:limit]
            next_cursor = None
            if has_more and page_events:
                last = page_events[-1]
                next_cursor = encode_event_cursor(
                    CollectionEventCursor(
                        job_id=job.id,
                        occurred_at=last.occurred_at,
                        event_id=last.id,
                    )
                )
            response = CollectionEventPageResponse(
                items=[event_response(event) for event in page_events],
                next_cursor=next_cursor,
            )
            await unit_of_work.commit()
            return response


def accepted_event(
    job: CollectionJob, task_count: int, occurred_at: datetime
) -> StoredCollectionEvent:
    return StoredCollectionEvent(
        collection_job_id=job.id,
        external_request_id=job.external_request_id,
        event_type=CollectionEventType.JOB_ACCEPTED,
        job_version=job.version,
        occurred_at=occurred_at,
        payload=CollectionJobAcceptedEventPayload(
            source=job.request_payload.source,
            task_count=task_count,
        ),
    )


def desired_state_changed_event(
    job: CollectionJob,
    action: str,
    occurred_at: datetime,
) -> StoredCollectionEvent:
    return StoredCollectionEvent(
        collection_job_id=job.id,
        external_request_id=job.external_request_id,
        event_type=CollectionEventType.JOB_DESIRED_STATE_CHANGED,
        job_version=job.version,
        occurred_at=occurred_at,
        payload=CollectionJobDesiredStateChangedEventPayload.model_validate(
            {
                "action": action,
                "desiredState": job.desired_state.value,
                "status": job.status.value,
            }
        ),
    )


def terminal_event(
    job: CollectionJob,
    tasks: list[CollectionTask],
    occurred_at: datetime,
) -> StoredCollectionEvent:
    if job.status not in TERMINAL_COLLECTION_STATES or job.completed_at is None:
        raise ValueError("terminal event requires a completed terminal collection job")
    failure_counts = Counter(
        task.failure_code for task in tasks if task.failure_code is not None
    )
    return StoredCollectionEvent(
        collection_job_id=job.id,
        external_request_id=job.external_request_id,
        event_type=CollectionEventType.JOB_TERMINAL,
        job_version=job.version,
        occurred_at=occurred_at,
        payload=CollectionJobTerminalEventPayload.model_validate(
            {
                "status": job.status.value,
                "processedCount": sum(task.processed_count for task in tasks),
                "failureCodeCounts": dict(failure_counts),
                "completedAt": job.completed_at,
            }
        ),
    )


def event_response(event: StoredCollectionEvent) -> CollectionEvent:
    return CollectionEvent(
        schema_version=1,
        event_id=event.id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        collection_job_id=event.collection_job_id,
        external_request_id=event.external_request_id,
        job_version=event.job_version,
        payload=event.payload,
    )


def encode_event_cursor(cursor: CollectionEventCursor) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "jobId": str(cursor.job_id),
            "occurredAt": cursor.occurred_at.isoformat(),
            "eventId": str(cursor.event_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_event_cursor(value: str, expected_job_id: UUID) -> CollectionEventCursor:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "jobId",
            "occurredAt",
            "eventId",
        }:
            raise ValueError
        if payload["v"] != 1:
            raise ValueError
        cursor = CollectionEventCursor(
            job_id=UUID(payload["jobId"]),
            occurred_at=datetime.fromisoformat(payload["occurredAt"]),
            event_id=UUID(payload["eventId"]),
        )
        if cursor.occurred_at.tzinfo is None or cursor.job_id != expected_job_id:
            raise ValueError
        return cursor
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise CollectionEventCursorInvalid from error

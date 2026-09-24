import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from crawler_api.domain.webhooks import (
    StoredWebhookDelivery,
    WebhookDeliveryReplay,
    WebhookOutboxStatus,
)
from crawler_contracts.common import ApiScope
from crawler_contracts.events import CollectionEventType
from crawler_contracts.webhooks import (
    ReplayWebhookDeliveryResponse,
    WebhookDelivery,
    WebhookDeliveryPageResponse,
)
from pydantic import SecretStr

from .caller_authentication import CallerAccessTokenAuthenticator
from .collection_jobs import InvalidIdempotencyKey, hash_idempotency_key
from .ports import UnitOfWork


class WebhookDeliveryNotFound(Exception):
    pass


class WebhookDeliveryCursorInvalid(Exception):
    pass


class WebhookDeliveryReplayConflict(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WebhookDeliveryCursor:
    job_id: UUID
    status: WebhookOutboxStatus
    created_at: datetime
    outbox_id: UUID


class WebhookDeliveryOperator:
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
        status: WebhookOutboxStatus,
        cursor: str | None,
        limit: int,
    ) -> WebhookDeliveryPageResponse:
        read_at = self._clock()
        position = decode_webhook_delivery_cursor(cursor, job_id, status) if cursor else None
        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_READ,
                authenticated_at=read_at,
            )
            job = await unit_of_work.collection_jobs.get_for_client(job_id, caller.client.id)
            if job is None:
                raise WebhookDeliveryNotFound
            deliveries = await unit_of_work.webhook_outbox.list_for_job(
                job.id,
                status=status,
                after_created_at=position.created_at if position else None,
                after_id=position.outbox_id if position else None,
                limit=limit + 1,
            )
            page_deliveries = deliveries[:limit]
            next_cursor = None
            if len(deliveries) > limit and page_deliveries:
                last = page_deliveries[-1]
                next_cursor = encode_webhook_delivery_cursor(
                    WebhookDeliveryCursor(
                        job_id=job.id,
                        status=status,
                        created_at=last.created_at,
                        outbox_id=last.id,
                    )
                )
            response = WebhookDeliveryPageResponse(
                items=[delivery_response(item, read_at) for item in page_deliveries],
                next_cursor=next_cursor,
            )
            await unit_of_work.commit()
            return response

    async def replay(
        self,
        *,
        access_token: SecretStr,
        idempotency_key: SecretStr,
        job_id: UUID,
        outbox_id: UUID,
    ) -> ReplayWebhookDeliveryResponse:
        raw_key = idempotency_key.get_secret_value()
        if not raw_key or len(raw_key) > 200:
            raise InvalidIdempotencyKey
        replayed_at = self._clock()
        key_hash = hash_idempotency_key(idempotency_key)
        async with self._unit_of_work_factory() as unit_of_work:
            caller = await self._authenticator.authenticate(
                unit_of_work=unit_of_work,
                access_token=access_token,
                required_scope=ApiScope.COLLECTION_CONTROL,
                authenticated_at=replayed_at,
            )
            delivery = await unit_of_work.webhook_outbox.get_for_client_for_update(
                outbox_id=outbox_id,
                job_id=job_id,
                client_id=caller.client.id,
            )
            if delivery is None:
                raise WebhookDeliveryNotFound
            existing = await unit_of_work.webhook_delivery_replays.get(outbox_id, key_hash)
            if existing is not None:
                result = replay_response(existing)
                await unit_of_work.commit()
                return result
            if not replay_eligible(delivery, replayed_at):
                raise WebhookDeliveryReplayConflict
            replay = WebhookDeliveryReplay(
                webhook_outbox_id=delivery.id,
                idempotency_key_hash=key_hash,
                replay_number=delivery.replay_count + 1,
                previous_attempt_count=delivery.attempt_count,
                previous_failure_code=delivery.last_failure_code or "",
                created_at=replayed_at,
            )
            await unit_of_work.webhook_delivery_replays.add(replay)
            if not await unit_of_work.webhook_outbox.activate_replay(
                delivery.id,
                expected_replay_count=delivery.replay_count,
                replayed_at=replayed_at,
            ):
                raise WebhookDeliveryReplayConflict
            await unit_of_work.commit()
            return replay_response(replay)


def replay_eligible(delivery: StoredWebhookDelivery, observed_at: datetime) -> bool:
    return (
        delivery.status is WebhookOutboxStatus.PERMANENTLY_FAILED
        and delivery.last_failure_code is not None
        and delivery.last_failure_code.startswith("retry_exhausted_")
        and delivery.attempt_count > 0
        and delivery.dispatch_claim_id is None
        and delivery.dispatch_lease_expires_at is None
        and delivery.credential_available
        and delivery.token_expires_at > observed_at
    )


def delivery_response(
    delivery: StoredWebhookDelivery, observed_at: datetime
) -> WebhookDelivery:
    active_claim = (
        delivery.dispatch_claim_id is not None
        and delivery.dispatch_lease_expires_at is not None
        and delivery.dispatch_lease_expires_at > observed_at
    )
    return WebhookDelivery(
        outbox_id=delivery.id,
        event_id=delivery.event_id,
        event_type=CollectionEventType(delivery.event_type),
        status=delivery.status.value,
        attempt_count=delivery.attempt_count,
        replay_count=delivery.replay_count,
        replay_eligible=replay_eligible(delivery, observed_at),
        next_attempt_at=delivery.next_attempt_at,
        delivered_at=delivery.delivered_at,
        last_failure_code=delivery.last_failure_code,
        active_claim=active_claim,
        claim_expires_at=delivery.dispatch_lease_expires_at if active_claim else None,
        last_replayed_at=delivery.last_replayed_at,
        created_at=delivery.created_at,
    )


def replay_response(replay: WebhookDeliveryReplay) -> ReplayWebhookDeliveryResponse:
    if replay.created_at is None:
        raise RuntimeError("persisted webhook delivery replay is missing created_at")
    return ReplayWebhookDeliveryResponse(
        outbox_id=replay.webhook_outbox_id,
        replay_number=replay.replay_number,
        replayed_at=replay.created_at,
        previous_attempt_count=replay.previous_attempt_count,
        previous_failure_code=replay.previous_failure_code,
    )


def encode_webhook_delivery_cursor(cursor: WebhookDeliveryCursor) -> str:
    value = json.dumps(
        {
            "jobId": str(cursor.job_id),
            "status": cursor.status.value,
            "createdAt": cursor.created_at.isoformat(),
            "outboxId": str(cursor.outbox_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_webhook_delivery_cursor(
    value: str,
    expected_job_id: UUID,
    expected_status: WebhookOutboxStatus,
) -> WebhookDeliveryCursor:
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(raw, dict) or set(raw) != {
            "jobId",
            "status",
            "createdAt",
            "outboxId",
        }:
            raise ValueError
        cursor = WebhookDeliveryCursor(
            job_id=UUID(raw["jobId"]),
            status=WebhookOutboxStatus(raw["status"]),
            created_at=datetime.fromisoformat(raw["createdAt"]),
            outbox_id=UUID(raw["outboxId"]),
        )
        if (
            cursor.created_at.tzinfo is None
            or cursor.job_id != expected_job_id
            or cursor.status is not expected_status
        ):
            raise ValueError
        return cursor
    except (
        AttributeError,
        ValueError,
        TypeError,
        KeyError,
        binascii.Error,
        UnicodeDecodeError,
    ) as error:
        raise WebhookDeliveryCursorInvalid from error

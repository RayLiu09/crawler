import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

from crawler_api.domain.webhooks import (
    ClaimedWebhookDelivery,
    WebhookDeliverySettlement,
    WebhookOutboxStatus,
)
from crawler_api.infrastructure.security.callback_tokens import (
    CallbackTokenProtector,
    ProtectedCallbackToken,
)
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr

from .collection_events import event_response
from .ports import UnitOfWork

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookHttpResponse:
    status_code: int
    headers: Mapping[str, str]


class WebhookTransportFailure(Exception):
    def __init__(self, failure_code: str) -> None:
        if failure_code not in {"connection_error", "timeout"}:
            raise ValueError("unsupported webhook transport failure code")
        super().__init__(failure_code)
        self.failure_code = failure_code


class WebhookTransport(Protocol):
    async def send(
        self,
        *,
        target_url: str,
        access_token: SecretStr,
        event_id: str,
        payload: dict[str, object],
    ) -> WebhookHttpResponse: ...


class WebhookTelemetry(Protocol):
    def record_claim(self, *, recovered: bool) -> None: ...

    def record_settlement(
        self,
        settlement: WebhookDeliverySettlement,
        *,
        duration_seconds: float,
    ) -> None: ...

    def record_stale_settlement(self) -> None: ...

    def record_dispatcher_error(self) -> None: ...


class NoopWebhookTelemetry:
    def record_claim(self, *, recovered: bool) -> None:
        pass

    def record_settlement(
        self,
        settlement: WebhookDeliverySettlement,
        *,
        duration_seconds: float,
    ) -> None:
        pass

    def record_stale_settlement(self) -> None:
        pass

    def record_dispatcher_error(self) -> None:
        pass


class WebhookDispatcher:
    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        callback_token_protector: CallbackTokenProtector,
        transport: WebhookTransport,
        claim_lease_seconds: int,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        retry_after_max_seconds: int,
        telemetry: WebhookTelemetry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._callback_token_protector = callback_token_protector
        self._transport = transport
        self._claim_lease = timedelta(seconds=claim_lease_seconds)
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._retry_after_max_seconds = retry_after_max_seconds
        self._telemetry = telemetry or NoopWebhookTelemetry()
        self._clock = clock
        self._monotonic_clock = monotonic_clock

    async def dispatch_one(self) -> bool:
        claimed_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            claim = await unit_of_work.webhook_outbox.claim_due(
                claim_id=uuid4(),
                claimed_at=claimed_at,
                lease_expires_at=claimed_at + self._claim_lease,
            )
            await unit_of_work.commit()
        if claim is None:
            return False

        self._telemetry.record_claim(recovered=claim.recovered_expired_claim)
        delivery_started_at = self._monotonic_clock()
        settlement = await self._deliver(claim)
        async with self._unit_of_work_factory() as unit_of_work:
            effective_settlement = await unit_of_work.webhook_outbox.settle_claim(settlement)
            await unit_of_work.commit()
        if effective_settlement is None:
            self._telemetry.record_stale_settlement()
            self._audit_settlement(claim, settlement, status="stale_claim")
            return True

        self._telemetry.record_settlement(
            effective_settlement,
            duration_seconds=max(0.0, self._monotonic_clock() - delivery_started_at),
        )
        self._audit_settlement(
            claim,
            effective_settlement,
            status=effective_settlement.status.value,
        )
        return True

    @staticmethod
    def _audit_settlement(
        claim: ClaimedWebhookDelivery,
        settlement: WebhookDeliverySettlement,
        *,
        status: str,
    ) -> None:
        audit_fields = {
            "auditEvent": "webhook_delivery_settled",
            "webhookOutboxId": str(claim.outbox_id),
            "eventId": str(claim.event.id),
            "collectionJobId": str(claim.event.collection_job_id),
            "credentialVersion": claim.callback_config.credential_version,
            "attemptNumber": claim.attempt_number,
            "deliveryStatus": status,
            "failureCode": settlement.failure_code,
            "httpAttempted": settlement.attempted,
            "recoveredExpiredClaim": claim.recovered_expired_claim,
        }
        logger.info(
            json.dumps(audit_fields, separators=(",", ":"), sort_keys=True),
            extra={
                "audit_event": "webhook_delivery_settled",
                "webhook_outbox_id": str(claim.outbox_id),
                "event_id": str(claim.event.id),
                "collection_job_id": str(claim.event.collection_job_id),
                "credential_version": claim.callback_config.credential_version,
                "attempt_number": claim.attempt_number,
                "delivery_status": status,
                "failure_code": settlement.failure_code,
                "http_attempted": settlement.attempted,
                "recovered_expired_claim": claim.recovered_expired_claim,
            },
        )

    async def _deliver(self, claim: ClaimedWebhookDelivery) -> WebhookDeliverySettlement:
        dispatched_at = self._clock()
        callback = claim.callback_config
        if (
            callback.credential_destroyed_at is not None
            or callback.token_ciphertext is None
            or callback.token_nonce is None
        ):
            return self._settlement(
                claim,
                status=WebhookOutboxStatus.CREDENTIAL_BLOCKED,
                failure_code="callback_credential_destroyed",
                attempted=False,
            )
        if callback.token_expires_at <= dispatched_at:
            return self._settlement(
                claim,
                status=WebhookOutboxStatus.CREDENTIAL_BLOCKED,
                failure_code="callback_token_expired",
                attempted=False,
            )

        try:
            access_token = self._callback_token_protector.decrypt(
                ProtectedCallbackToken(
                    ciphertext=callback.token_ciphertext,
                    nonce=callback.token_nonce,
                    fingerprint=callback.token_fingerprint,
                    encryption_key_version=callback.encryption_key_version,
                ),
                callback_config_id=callback.id,
                collection_job_id=callback.collection_job_id,
                credential_version=callback.credential_version,
            )
        except (InvalidTag, ValueError):
            return self._settlement(
                claim,
                status=WebhookOutboxStatus.CREDENTIAL_BLOCKED,
                failure_code="callback_credential_unavailable",
                attempted=False,
            )

        event = event_response(claim.event)
        try:
            response = await self._transport.send(
                target_url=callback.target_url,
                access_token=access_token,
                event_id=str(event.event_id),
                payload=event.model_dump(mode="json", by_alias=True),
            )
        except WebhookTransportFailure as error:
            return self._retry_or_exhaust(
                claim,
                failure_code=error.failure_code,
                retry_after=None,
            )

        status_code = response.status_code
        if 200 <= status_code < 300:
            return self._settlement(claim, status=WebhookOutboxStatus.DELIVERED)
        if status_code in {401, 403}:
            return self._settlement(
                claim,
                status=WebhookOutboxStatus.CREDENTIAL_BLOCKED,
                failure_code=f"http_{status_code}",
            )
        if status_code in {408, 425, 429} or 500 <= status_code < 600:
            retry_after = (
                parse_retry_after(response.headers.get("retry-after"), self._clock())
                if status_code in {429, 503}
                else None
            )
            return self._retry_or_exhaust(
                claim,
                failure_code=f"http_{status_code}",
                retry_after=retry_after,
            )
        return self._settlement(
            claim,
            status=WebhookOutboxStatus.PERMANENTLY_FAILED,
            failure_code=f"http_{status_code}",
        )

    def _retry_or_exhaust(
        self,
        claim: ClaimedWebhookDelivery,
        *,
        failure_code: str,
        retry_after: int | None,
    ) -> WebhookDeliverySettlement:
        if claim.attempt_number >= self._max_attempts:
            return self._settlement(
                claim,
                status=WebhookOutboxStatus.PERMANENTLY_FAILED,
                failure_code=f"retry_exhausted_{failure_code}",
            )
        exponential = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** min(claim.attempt_number - 1, 30)),
        )
        delay = exponential
        if retry_after is not None:
            delay = min(
                self._retry_max_seconds,
                max(exponential, min(retry_after, self._retry_after_max_seconds)),
            )
        settled_at = self._clock()
        return self._settlement(
            claim,
            status=WebhookOutboxStatus.PENDING,
            failure_code=failure_code,
            next_attempt_at=settled_at + timedelta(seconds=delay),
            settled_at=settled_at,
        )

    def _settlement(
        self,
        claim: ClaimedWebhookDelivery,
        *,
        status: WebhookOutboxStatus,
        failure_code: str | None = None,
        next_attempt_at: datetime | None = None,
        settled_at: datetime | None = None,
        attempted: bool = True,
    ) -> WebhookDeliverySettlement:
        return WebhookDeliverySettlement(
            outbox_id=claim.outbox_id,
            claim_id=claim.claim_id,
            claimed_credential_version=claim.callback_config.credential_version,
            status=status,
            settled_at=settled_at or self._clock(),
            next_attempt_at=next_attempt_at,
            failure_code=failure_code,
            attempted=attempted,
        )


def parse_retry_after(value: str | None, now: datetime) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0, int((retry_at - now).total_seconds()))


class WebhookDispatcherRunner:
    def __init__(
        self,
        dispatcher: WebhookDispatcher,
        *,
        concurrency: int,
        poll_interval_seconds: float,
        telemetry: WebhookTelemetry | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._concurrency = concurrency
        self._poll_interval_seconds = poll_interval_seconds
        self._telemetry = telemetry or NoopWebhookTelemetry()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("webhook Dispatcher is already running")
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._run_worker(), name=f"webhook-dispatcher-{index + 1}")
            for index in range(self._concurrency)
        ]

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks)
            self._tasks = []

    async def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                dispatched = await self._dispatcher.dispatch_one()
            except Exception as error:
                self._telemetry.record_dispatcher_error()
                logger.error(
                    json.dumps(
                        {
                            "auditEvent": "webhook_dispatcher_iteration_failed",
                            "errorType": type(error).__name__,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    extra={"error_type": type(error).__name__},
                )
                dispatched = False
            if not dispatched:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval_seconds)

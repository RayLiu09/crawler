import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from uuid import UUID, uuid4

import httpx
import pytest
from crawler_api.application.collection_events import accepted_event
from crawler_api.application.webhook_dispatcher import (
    WebhookDispatcher,
    WebhookHttpResponse,
    WebhookTelemetry,
    WebhookTransportFailure,
    parse_retry_after,
)
from crawler_api.domain.callbacks import CollectionJobCallbackConfig
from crawler_api.domain.entities import CollectionJob
from crawler_api.domain.webhooks import (
    ClaimedWebhookDelivery,
    WebhookDeliverySettlement,
    WebhookOutboxStatus,
)
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_api.infrastructure.webhooks.http import HttpxWebhookTransport
from crawler_contracts.collection_jobs import CollectionJobPayload
from pydantic import SecretStr

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
CALLBACK_TOKEN = "callback_dispatch_token_123456789012345678901234"
CALLBACK_ENCRYPTION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
CALLBACK_FINGERPRINT_KEY = "callback-fingerprint-key-that-is-at-least-32-bytes"


class FakeWebhookOutboxRepository:
    def __init__(
        self,
        claim: ClaimedWebhookDelivery | None,
        *,
        settle_result: bool = True,
    ) -> None:
        self.claim = claim
        self.settle_result = settle_result
        self.claim_calls: list[tuple[UUID, datetime, datetime]] = []
        self.settlements: list[WebhookDeliverySettlement] = []

    async def claim_due(
        self,
        *,
        claim_id: UUID,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> ClaimedWebhookDelivery | None:
        self.claim_calls.append((claim_id, claimed_at, lease_expires_at))
        if self.claim is None:
            return None
        claimed = ClaimedWebhookDelivery(
            outbox_id=self.claim.outbox_id,
            claim_id=claim_id,
            event=self.claim.event,
            callback_config=self.claim.callback_config,
            attempt_number=self.claim.attempt_number,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            recovered_expired_claim=self.claim.recovered_expired_claim,
        )
        self.claim = None
        return claimed

    async def settle_claim(
        self, settlement: WebhookDeliverySettlement
    ) -> WebhookDeliverySettlement | None:
        self.settlements.append(settlement)
        return settlement if self.settle_result else None


class FakeUnitOfWork:
    def __init__(self, repository: FakeWebhookOutboxRepository) -> None:
        self.webhook_outbox = repository
        self.commit_count = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


class FakeTransport:
    def __init__(self, result: WebhookHttpResponse | WebhookTransportFailure) -> None:
        self.result = result
        self.requests: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> WebhookHttpResponse:
        self.requests.append(kwargs)
        if isinstance(self.result, WebhookTransportFailure):
            raise self.result
        return self.result


class RecordingTelemetry(WebhookTelemetry):
    def __init__(self) -> None:
        self.recovered_claims = 0
        self.settlements: list[tuple[WebhookDeliverySettlement, float]] = []
        self.stale_settlements = 0
        self.dispatcher_errors = 0

    def record_claim(self, *, recovered: bool) -> None:
        self.recovered_claims += int(recovered)

    def record_settlement(
        self,
        settlement: WebhookDeliverySettlement,
        *,
        duration_seconds: float,
    ) -> None:
        self.settlements.append((settlement, duration_seconds))

    def record_stale_settlement(self) -> None:
        self.stale_settlements += 1

    def record_dispatcher_error(self) -> None:
        self.dispatcher_errors += 1


def callback_protector() -> CallbackTokenProtector:
    return CallbackTokenProtector(
        encryption_key=SecretStr(CALLBACK_ENCRYPTION_KEY),
        fingerprint_key=SecretStr(CALLBACK_FINGERPRINT_KEY),
        encryption_key_version="v1",
    )


def build_claim(
    *,
    attempt_number: int = 1,
    expires_at: datetime | None = None,
    corrupt_ciphertext: bool = False,
    destroyed_at: datetime | None = None,
    recovered_expired_claim: bool = False,
) -> ClaimedWebhookDelivery:
    job = CollectionJob(
        client_id=uuid4(),
        external_request_id="jobreq_dispatch_001",
        idempotency_key_hash="sha256:" + "a" * 64,
        request_payload=CollectionJobPayload.model_validate(
            {
                "schemaVersion": 1,
                "source": "zhaopin",
                "queries": [{"keyword": "Python", "region": "Shanghai"}],
            }
        ),
    )
    event = accepted_event(job, 1, NOW - timedelta(minutes=1))
    config_id = uuid4()
    protected = callback_protector().protect(
        SecretStr(CALLBACK_TOKEN),
        callback_config_id=config_id,
        collection_job_id=job.id,
        credential_version=1,
    )
    ciphertext = protected.ciphertext
    if corrupt_ciphertext:
        ciphertext = b"x" + ciphertext[1:]
    callback = CollectionJobCallbackConfig(
        id=config_id,
        collection_job_id=job.id,
        target_url="http://upstream.internal/api/job-collection/events",
        token_ciphertext=None if destroyed_at is not None else ciphertext,
        token_nonce=None if destroyed_at is not None else protected.nonce,
        token_fingerprint=protected.fingerprint,
        token_expires_at=expires_at or NOW + timedelta(days=1),
        encryption_key_version=protected.encryption_key_version,
        credential_destroyed_at=destroyed_at,
    )
    return ClaimedWebhookDelivery(
        outbox_id=uuid4(),
        claim_id=uuid4(),
        event=event,
        callback_config=callback,
        attempt_number=attempt_number,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
        recovered_expired_claim=recovered_expired_claim,
    )


def build_dispatcher(
    claim: ClaimedWebhookDelivery | None,
    transport: FakeTransport,
    *,
    max_attempts: int = 8,
    settle_result: bool = True,
    telemetry: WebhookTelemetry | None = None,
    monotonic_clock: Callable[[], float] = lambda: 0.0,
) -> tuple[WebhookDispatcher, FakeWebhookOutboxRepository, FakeUnitOfWork]:
    repository = FakeWebhookOutboxRepository(claim, settle_result=settle_result)
    unit_of_work = FakeUnitOfWork(repository)
    dispatcher = WebhookDispatcher(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[return-value]
        callback_token_protector=callback_protector(),
        transport=transport,
        claim_lease_seconds=30,
        max_attempts=max_attempts,
        retry_base_seconds=5,
        retry_max_seconds=900,
        retry_after_max_seconds=300,
        telemetry=telemetry,
        clock=lambda: NOW,
        monotonic_clock=monotonic_clock,
    )
    return dispatcher, repository, unit_of_work


async def test_success_sends_frozen_event_and_marks_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    claim = build_claim(recovered_expired_claim=True)
    transport = FakeTransport(WebhookHttpResponse(status_code=204, headers={}))
    telemetry = RecordingTelemetry()
    monotonic_values = iter([10.0, 12.5])
    dispatcher, repository, unit_of_work = build_dispatcher(
        claim,
        transport,
        telemetry=telemetry,
        monotonic_clock=lambda: next(monotonic_values),
    )

    assert await dispatcher.dispatch_one()

    assert unit_of_work.commit_count == 2
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["target_url"] == claim.callback_config.target_url
    assert isinstance(request["access_token"], SecretStr)
    assert request["access_token"].get_secret_value() == CALLBACK_TOKEN
    assert request["event_id"] == str(claim.event.id)
    assert request["payload"] == {
        "schemaVersion": 1,
        "eventId": str(claim.event.id),
        "eventType": "collection.job.accepted",
        "occurredAt": "2026-09-17T08:59:00Z",
        "collectionJobId": str(claim.event.collection_job_id),
        "externalRequestId": "jobreq_dispatch_001",
        "jobVersion": 1,
        "payload": {
            "source": "zhaopin",
            "desiredState": "running",
            "status": "queued",
            "taskCount": 1,
        },
    }
    settlement = repository.settlements[0]
    assert settlement.status is WebhookOutboxStatus.DELIVERED
    assert settlement.failure_code is None
    assert settlement.attempted
    assert telemetry.recovered_claims == 1
    assert telemetry.settlements == [(settlement, 2.5)]
    audit = next(record for record in caplog.records if record.audit_event)
    audit_payload = json.loads(audit.getMessage())
    assert audit.audit_event == "webhook_delivery_settled"
    assert audit.webhook_outbox_id == str(claim.outbox_id)
    assert audit.event_id == str(claim.event.id)
    assert audit.collection_job_id == str(claim.event.collection_job_id)
    assert audit.delivery_status == "delivered"
    assert audit_payload["auditEvent"] == "webhook_delivery_settled"
    assert audit_payload["deliveryStatus"] == "delivered"
    assert CALLBACK_TOKEN not in caplog.text
    assert claim.callback_config.target_url not in caplog.text


async def test_stale_claim_settlement_is_counted_without_a_delivery_result() -> None:
    telemetry = RecordingTelemetry()
    dispatcher, _, _ = build_dispatcher(
        build_claim(),
        FakeTransport(WebhookHttpResponse(status_code=204, headers={})),
        settle_result=False,
        telemetry=telemetry,
    )

    assert await dispatcher.dispatch_one()

    assert telemetry.stale_settlements == 1
    assert telemetry.settlements == []


@pytest.mark.parametrize(
    ("status_code", "expected_status", "failure_code"),
    [
        (401, WebhookOutboxStatus.CREDENTIAL_BLOCKED, "http_401"),
        (403, WebhookOutboxStatus.CREDENTIAL_BLOCKED, "http_403"),
        (302, WebhookOutboxStatus.PERMANENTLY_FAILED, "http_302"),
        (400, WebhookOutboxStatus.PERMANENTLY_FAILED, "http_400"),
    ],
)
async def test_non_retryable_http_results_are_classified(
    status_code: int,
    expected_status: WebhookOutboxStatus,
    failure_code: str,
) -> None:
    dispatcher, repository, _ = build_dispatcher(
        build_claim(),
        FakeTransport(WebhookHttpResponse(status_code=status_code, headers={})),
    )

    await dispatcher.dispatch_one()

    settlement = repository.settlements[0]
    assert settlement.status is expected_status
    assert settlement.failure_code == failure_code
    assert settlement.next_attempt_at is None


async def test_retry_after_is_bounded_and_not_shorter_than_exponential() -> None:
    dispatcher, repository, _ = build_dispatcher(
        build_claim(attempt_number=2),
        FakeTransport(WebhookHttpResponse(status_code=503, headers={"retry-after": "600"})),
    )

    await dispatcher.dispatch_one()

    settlement = repository.settlements[0]
    assert settlement.status is WebhookOutboxStatus.PENDING
    assert settlement.failure_code == "http_503"
    assert settlement.next_attempt_at == NOW + timedelta(seconds=300)


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 599])
async def test_retryable_statuses_use_exponential_backoff(status_code: int) -> None:
    dispatcher, repository, _ = build_dispatcher(
        build_claim(attempt_number=3),
        FakeTransport(WebhookHttpResponse(status_code=status_code, headers={})),
    )

    await dispatcher.dispatch_one()

    settlement = repository.settlements[0]
    assert settlement.status is WebhookOutboxStatus.PENDING
    assert settlement.next_attempt_at == NOW + timedelta(seconds=20)


async def test_last_retryable_attempt_becomes_permanent_failure() -> None:
    dispatcher, repository, _ = build_dispatcher(
        build_claim(attempt_number=8),
        FakeTransport(WebhookTransportFailure("timeout")),
        max_attempts=8,
    )

    await dispatcher.dispatch_one()

    settlement = repository.settlements[0]
    assert settlement.status is WebhookOutboxStatus.PERMANENTLY_FAILED
    assert settlement.failure_code == "retry_exhausted_timeout"


@pytest.mark.parametrize(
    ("claim", "failure_code"),
    [
        (build_claim(expires_at=NOW), "callback_token_expired"),
        (build_claim(corrupt_ciphertext=True), "callback_credential_unavailable"),
    ],
)
async def test_unavailable_credentials_block_without_an_http_attempt(
    claim: ClaimedWebhookDelivery,
    failure_code: str,
) -> None:
    transport = FakeTransport(WebhookHttpResponse(status_code=204, headers={}))
    dispatcher, repository, _ = build_dispatcher(claim, transport)

    await dispatcher.dispatch_one()

    assert transport.requests == []
    settlement = repository.settlements[0]
    assert settlement.status is WebhookOutboxStatus.CREDENTIAL_BLOCKED
    assert settlement.failure_code == failure_code
    assert not settlement.attempted


async def test_destroyed_credential_blocks_without_decryption_or_http() -> None:
    transport = FakeTransport(WebhookHttpResponse(status_code=204, headers={}))
    dispatcher, repository, _ = build_dispatcher(
        build_claim(destroyed_at=NOW - timedelta(seconds=1)),
        transport,
    )

    await dispatcher.dispatch_one()

    assert transport.requests == []
    settlement = repository.settlements[0]
    assert settlement.status is WebhookOutboxStatus.CREDENTIAL_BLOCKED
    assert settlement.failure_code == "callback_credential_destroyed"
    assert not settlement.attempted


async def test_no_due_delivery_commits_claim_transaction_without_http() -> None:
    transport = FakeTransport(WebhookHttpResponse(status_code=204, headers={}))
    dispatcher, repository, unit_of_work = build_dispatcher(None, transport)

    assert not await dispatcher.dispatch_one()
    assert unit_of_work.commit_count == 1
    assert repository.settlements == []
    assert transport.requests == []


def test_retry_after_parses_seconds_dates_and_invalid_values() -> None:
    assert parse_retry_after("120", NOW) == 120
    assert parse_retry_after(format_datetime(NOW + timedelta(seconds=90)), NOW) == 90
    assert parse_retry_after(format_datetime(NOW - timedelta(seconds=10)), NOW) == 0
    assert parse_retry_after("not-a-date", NOW) is None
    assert parse_retry_after(None, NOW) is None


async def test_http_transport_sends_headers_json_and_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://other.internal"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpxWebhookTransport(
        timeout_seconds=10,
        max_connections=1,
        client=client,
    )
    payload = {"schemaVersion": 1, "eventId": str(uuid4())}
    try:
        response = await transport.send(
            target_url="http://upstream.internal/callback",
            access_token=SecretStr(CALLBACK_TOKEN),
            event_id="event-id",
            payload=payload,
        )
    finally:
        await client.aclose()

    assert response.status_code == 302
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {CALLBACK_TOKEN}"
    assert requests[0].headers["Idempotency-Key"] == "event-id"
    assert json.loads(requests[0].content) == payload


async def test_http_transport_suppresses_dependency_logs_that_contain_callback_urls() -> None:
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(204)))

    try:
        HttpxWebhookTransport(timeout_seconds=10, max_connections=1, client=client)

        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("transport_error", "failure_code"),
    [
        (httpx.ReadTimeout("timeout"), "timeout"),
        (httpx.ConnectError("connection"), "connection_error"),
    ],
)
async def test_http_transport_sanitizes_network_failures(
    transport_error: httpx.TransportError,
    failure_code: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise transport_error

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpxWebhookTransport(
        timeout_seconds=10,
        max_connections=1,
        client=client,
    )
    try:
        with pytest.raises(WebhookTransportFailure) as captured:
            await transport.send(
                target_url="http://upstream.internal/callback",
                access_token=SecretStr(CALLBACK_TOKEN),
                event_id="event-id",
                payload={"schemaVersion": 1},
            )
    finally:
        await client.aclose()

    assert captured.value.failure_code == failure_code
    assert str(captured.value) == failure_code


def test_transport_failure_rejects_unbounded_failure_codes() -> None:
    with pytest.raises(ValueError, match="unsupported webhook transport failure code"):
        WebhookTransportFailure("callback-token-in-error")

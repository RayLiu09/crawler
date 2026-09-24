import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest
from crawler_api.application.access_tokens import AccessTokenIssuer
from crawler_api.application.callback_token_rotation import CollectionJobCallbackTokenRotator
from crawler_api.application.caller_authentication import CallerAccessTokenAuthenticator
from crawler_api.application.collection_jobs import CollectionJobSubmitter
from crawler_api.application.tenant_allowlist import TenantAllowlist, TenantAllowlistSynchronizer
from crawler_api.application.webhook_dispatcher import (
    WebhookDispatcher,
    WebhookTelemetry,
)
from crawler_api.config import get_settings
from crawler_api.domain.webhooks import WebhookDeliverySettlement, WebhookOutboxStatus
from crawler_api.infrastructure.database.models import CollectionEventRow, WebhookOutboxRow
from crawler_api.infrastructure.database.session import create_database_engine
from crawler_api.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from crawler_api.infrastructure.security.callback_tokens import CallbackTokenProtector
from crawler_api.infrastructure.security.token_hashing import AccessTokenHasher
from crawler_api.infrastructure.webhooks.http import HttpxWebhookTransport
from crawler_contracts.collection_jobs import (
    RotateCollectionJobCallbackTokenRequest,
    SubmitCollectionJobRequest,
)
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires an explicitly selected migrated PostgreSQL database and loopback TCP",
)

BASE_TIME = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
CALLBACK_TOKEN = "callback_e2e_initial_token_123456789012345678901234"
ROTATED_CALLBACK_TOKEN = "callback_e2e_rotated_token_98765432109876543210"
SENSITIVE_RESPONSE_BODY = "upstream-sensitive-response-body"


@dataclass(frozen=True, slots=True)
class CallbackBehavior:
    status_code: int = 204
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    delay_seconds: float = 0.0
    disconnect: bool = False


@dataclass(frozen=True, slots=True)
class ReceivedCallback:
    path: str
    headers: dict[str, str]
    payload: dict[str, object]


class FaultingCallbackServer:
    def __init__(self, behaviors: list[CallbackBehavior]) -> None:
        self._behaviors = behaviors
        self._server: asyncio.Server | None = None
        self._handler_tasks: set[asyncio.Task[object]] = set()
        self._request_condition = asyncio.Condition()
        self.requests: list[ReceivedCallback] = []
        self.url = ""

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        socket = self._server.sockets[0]
        port = int(socket.getsockname()[1])
        self.url = f"http://127.0.0.1:{port}/callbacks/collection-events"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)

    async def wait_for_requests(self, count: int) -> None:
        async with self._request_condition:
            await asyncio.wait_for(
                self._request_condition.wait_for(lambda: len(self.requests) >= count),
                timeout=2,
            )

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            lines = raw_head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            if method != "POST":
                raise AssertionError(f"unexpected callback method: {method}")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            content_length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(content_length)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise AssertionError("callback payload must be an object")
            async with self._request_condition:
                self.requests.append(ReceivedCallback(path=path, headers=headers, payload=payload))
                self._request_condition.notify_all()

            behavior = self._behaviors.pop(0)
            if behavior.disconnect:
                writer.transport.abort()
                return
            if behavior.delay_seconds:
                await asyncio.sleep(behavior.delay_seconds)
            reason = {
                204: "No Content",
                307: "Temporary Redirect",
                401: "Unauthorized",
                503: "Service Unavailable",
            }.get(behavior.status_code, "Test Response")
            response_body = behavior.body.encode()
            response_headers = {
                "Content-Length": str(len(response_body)),
                "Connection": "close",
                **behavior.headers,
            }
            response = [f"HTTP/1.1 {behavior.status_code} {reason}"]
            response.extend(f"{name}: {value}" for name, value in response_headers.items())
            writer.write(("\r\n".join(response) + "\r\n\r\n").encode() + response_body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            if task is not None:
                self._handler_tasks.discard(task)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingTelemetry(WebhookTelemetry):
    def __init__(self) -> None:
        self.recovered_claims = 0
        self.settlements: list[WebhookDeliverySettlement] = []
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
        assert duration_seconds >= 0
        self.settlements.append(settlement)

    def record_stale_settlement(self) -> None:
        self.stale_settlements += 1

    def record_dispatcher_error(self) -> None:
        self.dispatcher_errors += 1


@dataclass(slots=True)
class CallbackHarness:
    engine: AsyncEngine
    outer_transaction: object
    session_factory: async_sessionmaker[AsyncSession]
    unit_of_work_factory: object
    authenticator: CallerAccessTokenAuthenticator
    protector: CallbackTokenProtector
    submitter: CollectionJobSubmitter
    access_token: str
    callback_url: str

    async def submit(self, suffix: str) -> object:
        return await self.submitter.submit(
            access_token=SecretStr(self.access_token),
            idempotency_key=SecretStr(f"webhook-e2e-create-{suffix}"),
            request=SubmitCollectionJobRequest.model_validate(
                {
                    "externalRequestId": f"jobreq_webhook_e2e_{suffix}",
                    "source": "zhaopin",
                    "queries": [{"keyword": "Python", "region": "Shanghai"}],
                    "callback": {
                        "url": self.callback_url,
                        "accessToken": CALLBACK_TOKEN,
                        "tokenType": "Bearer",
                    },
                }
            ),
        )


def build_allowlist(callback_url: str, suffix: str) -> TenantAllowlist:
    tenant_key = f"webhook-e2e-tenant-key-{suffix}"
    return TenantAllowlist.model_validate(
        {
            "version": 1,
            "tenants": [
                {
                    "tenantId": f"webhook-e2e-tenant-{suffix}",
                    "displayName": "Webhook Callback E2E Tenant",
                    "tenantKeyHashes": [
                        f"sha256:{hashlib.sha256(tenant_key.encode()).hexdigest()}"
                    ],
                    "callbackAllowlist": [callback_url],
                }
            ],
        }
    )


@asynccontextmanager
async def callback_harness(
    callback_url: str,
    suffix: str,
) -> AsyncIterator[CallbackHarness]:
    settings = get_settings()
    engine = create_database_engine(settings)
    connection = await engine.connect()
    outer_transaction = await connection.begin()
    session_factory = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )

    def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    allowlist = build_allowlist(callback_url, suffix)
    tenant_key = f"webhook-e2e-tenant-key-{suffix}"
    token_hasher = AccessTokenHasher(settings.api_token_hash_key)
    protector = CallbackTokenProtector(
        encryption_key=settings.callback_token_encryption_key,
        fingerprint_key=settings.callback_token_fingerprint_key,
        encryption_key_version=settings.callback_token_encryption_key_version,
    )
    try:
        await TenantAllowlistSynchronizer(unit_of_work_factory).synchronize(allowlist)
        issued = await AccessTokenIssuer(
            allowlist=allowlist,
            unit_of_work_factory=unit_of_work_factory,
            hash_token=token_hasher.hash,
            ttl_seconds=7200,
            clock=lambda: BASE_TIME,
            token_generator=lambda: f"cat_webhook_e2e_{suffix}_token_1234567890123456789",
        ).issue(SecretStr(tenant_key))
        authenticator = CallerAccessTokenAuthenticator(token_hasher.hash)
        submitter = CollectionJobSubmitter(
            unit_of_work_factory=unit_of_work_factory,
            authenticator=authenticator,
            tenant_allowlist=allowlist,
            callback_token_protector=protector,
            clock=lambda: BASE_TIME + timedelta(minutes=1),
        )
        yield CallbackHarness(
            engine=engine,
            outer_transaction=outer_transaction,
            session_factory=session_factory,
            unit_of_work_factory=unit_of_work_factory,
            authenticator=authenticator,
            protector=protector,
            submitter=submitter,
            access_token=issued.access_token,
            callback_url=callback_url,
        )
    finally:
        await outer_transaction.rollback()
        await connection.close()
        await engine.dispose()


def build_dispatcher(
    harness: CallbackHarness,
    transport: HttpxWebhookTransport,
    clock: MutableClock,
    *,
    claim_lease_seconds: int = 1,
    telemetry: WebhookTelemetry | None = None,
) -> WebhookDispatcher:
    return WebhookDispatcher(
        unit_of_work_factory=harness.unit_of_work_factory,  # type: ignore[arg-type]
        callback_token_protector=harness.protector,
        transport=transport,
        claim_lease_seconds=claim_lease_seconds,
        max_attempts=8,
        retry_base_seconds=1,
        retry_max_seconds=30,
        retry_after_max_seconds=10,
        telemetry=telemetry,
        clock=clock,
    )


async def load_outbox(harness: CallbackHarness, collection_job_id: object) -> WebhookOutboxRow:
    async with harness.session_factory() as session:
        row = await session.scalar(
            select(WebhookOutboxRow)
            .join(CollectionEventRow, CollectionEventRow.id == WebhookOutboxRow.event_id)
            .where(CollectionEventRow.collection_job_id == collection_job_id)
        )
    assert row is not None
    return row


async def test_real_callback_faults_are_classified_bounded_and_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    behaviors = [
        CallbackBehavior(
            status_code=503,
            headers={"Retry-After": "2"},
            body=SENSITIVE_RESPONSE_BODY,
        ),
        CallbackBehavior(disconnect=True),
        CallbackBehavior(status_code=503, delay_seconds=0.2),
        CallbackBehavior(
            status_code=307,
            headers={"Location": "/must-not-be-followed"},
        ),
    ]
    async with (
        FaultingCallbackServer(behaviors) as callback_server,
        callback_harness(callback_server.url, "faults") as harness,
    ):
        submitted = await harness.submit("faults_001")
        clock = MutableClock(BASE_TIME + timedelta(minutes=2))
        transport = HttpxWebhookTransport(timeout_seconds=0.05, max_connections=2)
        dispatcher = build_dispatcher(harness, transport, clock)
        try:
            assert await dispatcher.dispatch_one()
            first = await load_outbox(harness, submitted.job.id)
            assert first.status == WebhookOutboxStatus.PENDING.value
            assert first.attempt_count == 1
            assert first.last_failure_code == "http_503"
            assert first.next_attempt_at == clock.now + timedelta(seconds=2)

            clock.now = first.next_attempt_at
            assert await dispatcher.dispatch_one()
            second = await load_outbox(harness, submitted.job.id)
            assert second.status == WebhookOutboxStatus.PENDING.value
            assert second.attempt_count == 2
            assert second.last_failure_code == "connection_error"
            assert second.next_attempt_at == clock.now + timedelta(seconds=2)

            clock.now = second.next_attempt_at
            assert await dispatcher.dispatch_one()
            third = await load_outbox(harness, submitted.job.id)
            assert third.status == WebhookOutboxStatus.PENDING.value
            assert third.attempt_count == 3
            assert third.last_failure_code == "timeout"
            assert third.next_attempt_at == clock.now + timedelta(seconds=4)

            clock.now = third.next_attempt_at
            assert await dispatcher.dispatch_one()
            failed = await load_outbox(harness, submitted.job.id)
            assert failed.status == WebhookOutboxStatus.PERMANENTLY_FAILED.value
            assert failed.attempt_count == 4
            assert failed.last_failure_code == "http_307"
        finally:
            await transport.close()

        assert len(callback_server.requests) == 4
        assert not behaviors
        event_ids = {request.headers["idempotency-key"] for request in callback_server.requests}
        assert len(event_ids) == 1
        assert all(
            request.path == "/callbacks/collection-events" for request in callback_server.requests
        )
        assert all(
            request.headers["authorization"] == f"Bearer {CALLBACK_TOKEN}"
            for request in callback_server.requests
        )
        assert all(
            request.payload["eventId"] == request.headers["idempotency-key"]
            for request in callback_server.requests
        )
        assert all(
            request.payload["eventType"] == "collection.job.accepted"
            for request in callback_server.requests
        )
        assert callback_server.url not in caplog.text
        assert CALLBACK_TOKEN not in caplog.text
        assert SENSITIVE_RESPONSE_BODY not in caplog.text


async def test_real_callback_credential_rotation_recovers_same_event() -> None:
    behaviors = [
        CallbackBehavior(status_code=401),
        CallbackBehavior(status_code=204),
    ]
    async with (
        FaultingCallbackServer(behaviors) as callback_server,
        callback_harness(callback_server.url, "rotation") as harness,
    ):
        submitted = await harness.submit("rotation_001")
        clock = MutableClock(BASE_TIME + timedelta(minutes=2))
        transport = HttpxWebhookTransport(timeout_seconds=1, max_connections=1)
        dispatcher = build_dispatcher(harness, transport, clock)
        try:
            assert await dispatcher.dispatch_one()
            blocked = await load_outbox(harness, submitted.job.id)
            assert blocked.status == WebhookOutboxStatus.CREDENTIAL_BLOCKED.value
            assert blocked.attempt_count == 1
            assert blocked.last_failure_code == "http_401"

            clock.now += timedelta(minutes=1)
            await CollectionJobCallbackTokenRotator(
                unit_of_work_factory=harness.unit_of_work_factory,  # type: ignore[arg-type]
                authenticator=harness.authenticator,
                callback_token_protector=harness.protector,
                clock=clock,
            ).rotate(
                access_token=SecretStr(harness.access_token),
                idempotency_key=SecretStr("webhook-e2e-rotation-001"),
                job_id=submitted.job.id,
                request=RotateCollectionJobCallbackTokenRequest(
                    accessToken=ROTATED_CALLBACK_TOKEN,
                    tokenType="Bearer",
                ),
            )
            resumed = await load_outbox(harness, submitted.job.id)
            assert resumed.status == WebhookOutboxStatus.PENDING.value
            assert resumed.attempt_count == 1
            assert resumed.next_attempt_at == clock.now
            assert resumed.last_failure_code is None

            assert await dispatcher.dispatch_one()
            delivered = await load_outbox(harness, submitted.job.id)
            assert delivered.status == WebhookOutboxStatus.DELIVERED.value
            assert delivered.attempt_count == 2
        finally:
            await transport.close()

        assert [request.headers["authorization"] for request in callback_server.requests] == [
            f"Bearer {CALLBACK_TOKEN}",
            f"Bearer {ROTATED_CALLBACK_TOKEN}",
        ]
        assert (
            len({request.headers["idempotency-key"] for request in callback_server.requests}) == 1
        )


async def test_expired_claim_is_recovered_and_late_http_result_is_fenced() -> None:
    behaviors = [
        CallbackBehavior(status_code=503, delay_seconds=0.2),
        CallbackBehavior(status_code=204),
    ]
    async with (
        FaultingCallbackServer(behaviors) as callback_server,
        callback_harness(callback_server.url, "claim-race") as harness,
    ):
        submitted = await harness.submit("claim_race_001")
        clock = MutableClock(BASE_TIME + timedelta(minutes=2))
        telemetry = RecordingTelemetry()
        transport = HttpxWebhookTransport(timeout_seconds=1, max_connections=2)
        dispatcher = build_dispatcher(
            harness,
            transport,
            clock,
            claim_lease_seconds=1,
            telemetry=telemetry,
        )
        try:
            first_dispatch = asyncio.create_task(dispatcher.dispatch_one())
            await callback_server.wait_for_requests(1)
            clock.now += timedelta(seconds=1)
            assert await dispatcher.dispatch_one()
            assert await first_dispatch
        finally:
            await transport.close()

        delivered = await load_outbox(harness, submitted.job.id)
        assert delivered.status == WebhookOutboxStatus.DELIVERED.value
        assert delivered.attempt_count == 1
        assert telemetry.recovered_claims == 1
        assert telemetry.stale_settlements == 1
        assert len(telemetry.settlements) == 1
        assert telemetry.settlements[0].status is WebhookOutboxStatus.DELIVERED
        assert len(callback_server.requests) == 2
        assert (
            len({request.headers["idempotency-key"] for request in callback_server.requests}) == 1
        )

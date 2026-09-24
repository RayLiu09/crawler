from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from crawler_api.api.v1.internal_metrics import router
from crawler_api.domain.webhooks import (
    WebhookDeliverySettlement,
    WebhookOutboxStatus,
    WebhookQueueMetricsSnapshot,
)
from crawler_api.infrastructure.observability import WebhookPrometheusMetrics
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client.parser import text_string_to_metric_families

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


class FakeWebhookOutboxRepository:
    def __init__(self, snapshot: WebhookQueueMetricsSnapshot) -> None:
        self.snapshot = snapshot
        self.observed_at: datetime | None = None

    async def get_metrics_snapshot(
        self, observed_at: datetime
    ) -> WebhookQueueMetricsSnapshot:
        self.observed_at = observed_at
        return self.snapshot


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


def settlement(
    status: WebhookOutboxStatus,
    *,
    failure_code: str | None = None,
) -> WebhookDeliverySettlement:
    return WebhookDeliverySettlement(
        outbox_id=uuid4(),
        claim_id=uuid4(),
        claimed_credential_version=1,
        status=status,
        settled_at=NOW,
        failure_code=failure_code,
    )


def sample_values(payload: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for family in text_string_to_metric_families(payload):
        for sample in family.samples:
            values[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return values


async def test_prometheus_metrics_combine_runtime_counters_and_database_snapshot() -> None:
    snapshot = WebhookQueueMetricsSnapshot(
        pending_count=4,
        oldest_pending_created_at=NOW - timedelta(seconds=75),
        credential_blocked_count=2,
        permanently_failed_count=3,
        retry_exhausted_count=1,
        active_claim_count=2,
    )
    repository = FakeWebhookOutboxRepository(snapshot)
    unit_of_work = FakeUnitOfWork(repository)
    metrics = WebhookPrometheusMetrics(
        unit_of_work_factory=lambda: unit_of_work,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    metrics.record_claim(recovered=True)
    metrics.record_claim(recovered=False)
    metrics.record_settlement(
        settlement(WebhookOutboxStatus.DELIVERED), duration_seconds=0.25
    )
    metrics.record_settlement(
        settlement(WebhookOutboxStatus.PENDING, failure_code="http_503"),
        duration_seconds=0.5,
    )
    metrics.record_settlement(
        settlement(
            WebhookOutboxStatus.PERMANENTLY_FAILED,
            failure_code="retry_exhausted_timeout",
        ),
        duration_seconds=1.0,
    )
    metrics.record_stale_settlement()
    metrics.record_dispatcher_error()

    payload = (await metrics.render_latest()).decode("utf-8")
    samples = sample_values(payload)

    assert repository.observed_at == NOW
    assert unit_of_work.commit_count == 1
    assert samples[("crawler_webhook_outbox_pending", ())] == 4
    assert samples[("crawler_webhook_outbox_oldest_pending_age_seconds", ())] == 75
    assert samples[("crawler_webhook_outbox_credential_blocked", ())] == 2
    assert samples[("crawler_webhook_outbox_permanently_failed", ())] == 3
    assert samples[("crawler_webhook_outbox_retry_exhausted", ())] == 1
    assert samples[("crawler_webhook_outbox_active_claims", ())] == 2
    assert samples[("crawler_webhook_claim_recovered_total", ())] == 1
    assert samples[("crawler_webhook_stale_settlement_total", ())] == 1
    assert samples[("crawler_webhook_retry_exhausted_total", ())] == 1
    assert samples[("crawler_webhook_dispatcher_error_total", ())] == 1
    assert samples[("crawler_webhook_delivery_total", (("result", "delivered"),))] == 1
    assert (
        samples[("crawler_webhook_delivery_total", (("result", "retry_scheduled"),))]
        == 1
    )
    assert "callback.example.internal" not in payload
    assert "callback-secret" not in payload


class StubExporter:
    content_type = "text/plain; version=0.0.4; charset=utf-8"

    async def render_latest(self) -> bytes:
        return b"crawler_webhook_outbox_pending 1.0\n"


class FailingExporter:
    content_type = "text/plain"

    async def render_latest(self) -> bytes:
        raise RuntimeError("callback-secret-must-not-be-logged")


def build_app(exporter: object | None) -> FastAPI:
    app = FastAPI()
    if exporter is not None:
        app.state.webhook_metrics = exporter
    app.include_router(router)
    return app


async def test_internal_metrics_route_is_not_in_openapi_and_uses_prometheus_type() -> None:
    app = build_app(StubExporter())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/internal/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == StubExporter.content_type
    assert response.text == "crawler_webhook_outbox_pending 1.0\n"
    assert "/internal/metrics" not in app.openapi()["paths"]


async def test_internal_metrics_route_can_be_disabled() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=build_app(None)), base_url="http://test"
    ) as client:
        response = await client.get("/internal/metrics")

    assert response.status_code == 404


async def test_internal_metrics_failure_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=build_app(FailingExporter())),
        base_url="http://test",
    ) as client:
        response = await client.get("/internal/metrics")

    assert response.status_code == 503
    assert response.text == "metrics unavailable\n"
    assert "callback-secret-must-not-be-logged" not in caplog.text

from collections.abc import Callable
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest

from crawler_api.application.ports import UnitOfWork
from crawler_api.domain.webhooks import WebhookDeliverySettlement, WebhookOutboxStatus


class WebhookPrometheusMetrics:
    content_type = CONTENT_TYPE_LATEST

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock
        self._registry = CollectorRegistry(auto_describe=True)
        self._delivery_total = Counter(
            "crawler_webhook_delivery_total",
            "Webhook delivery settlements accepted by the Outbox repository.",
            ("result",),
            registry=self._registry,
        )
        self._delivery_duration = Histogram(
            "crawler_webhook_delivery_duration_seconds",
            "Time from a committed Outbox claim to its accepted settlement.",
            ("result",),
            registry=self._registry,
        )
        self._claim_recovered_total = Counter(
            "crawler_webhook_claim_recovered_total",
            "Expired webhook dispatch claims recovered by this API process.",
            registry=self._registry,
        )
        self._stale_settlement_total = Counter(
            "crawler_webhook_stale_settlement_total",
            "Webhook settlements rejected because claim ownership changed.",
            registry=self._registry,
        )
        self._retry_exhausted_total = Counter(
            "crawler_webhook_retry_exhausted_total",
            "Webhook deliveries that exhausted their retry budget.",
            registry=self._registry,
        )
        self._dispatcher_error_total = Counter(
            "crawler_webhook_dispatcher_error_total",
            "Unexpected Dispatcher iteration failures.",
            registry=self._registry,
        )
        self._pending = Gauge(
            "crawler_webhook_outbox_pending",
            "Current number of pending webhook Outbox rows.",
            registry=self._registry,
        )
        self._oldest_pending_age = Gauge(
            "crawler_webhook_outbox_oldest_pending_age_seconds",
            "Age in seconds of the oldest pending webhook Outbox row.",
            registry=self._registry,
        )
        self._credential_blocked = Gauge(
            "crawler_webhook_outbox_credential_blocked",
            "Current number of credential-blocked webhook Outbox rows.",
            registry=self._registry,
        )
        self._permanently_failed = Gauge(
            "crawler_webhook_outbox_permanently_failed",
            "Current number of permanently failed webhook Outbox rows.",
            registry=self._registry,
        )
        self._retry_exhausted = Gauge(
            "crawler_webhook_outbox_retry_exhausted",
            "Current number of retry-exhausted webhook Outbox rows.",
            registry=self._registry,
        )
        self._active_claims = Gauge(
            "crawler_webhook_outbox_active_claims",
            "Current number of pending webhook Outbox rows with active claims.",
            registry=self._registry,
        )

    def record_claim(self, *, recovered: bool) -> None:
        if recovered:
            self._claim_recovered_total.inc()

    def record_settlement(
        self,
        settlement: WebhookDeliverySettlement,
        *,
        duration_seconds: float,
    ) -> None:
        result = (
            "retry_scheduled"
            if settlement.status is WebhookOutboxStatus.PENDING
            else settlement.status.value
        )
        self._delivery_total.labels(result=result).inc()
        self._delivery_duration.labels(result=result).observe(duration_seconds)
        if settlement.failure_code is not None and settlement.failure_code.startswith(
            "retry_exhausted_"
        ):
            self._retry_exhausted_total.inc()

    def record_stale_settlement(self) -> None:
        self._stale_settlement_total.inc()

    def record_dispatcher_error(self) -> None:
        self._dispatcher_error_total.inc()

    async def render_latest(self) -> bytes:
        observed_at = self._clock()
        async with self._unit_of_work_factory() as unit_of_work:
            snapshot = await unit_of_work.webhook_outbox.get_metrics_snapshot(observed_at)
            await unit_of_work.commit()

        self._pending.set(snapshot.pending_count)
        oldest_age = (
            max(0.0, (observed_at - snapshot.oldest_pending_created_at).total_seconds())
            if snapshot.oldest_pending_created_at is not None
            else 0.0
        )
        self._oldest_pending_age.set(oldest_age)
        self._credential_blocked.set(snapshot.credential_blocked_count)
        self._permanently_failed.set(snapshot.permanently_failed_count)
        self._retry_exhausted.set(snapshot.retry_exhausted_count)
        self._active_claims.set(snapshot.active_claim_count)
        return generate_latest(self._registry)

# Webhook monitoring

`GET /internal/metrics` exposes only aggregate Prometheus metrics and is omitted
from OpenAPI. It does not accept a tenant Caller Token. Production ingress or
network policy must allow this path only from the monitoring network; do not
publish it as a caller-facing API.

Set `WEBHOOK_METRICS_ENABLED=false` to make the endpoint return 404. Database-backed
gauges remain correct after an API restart. Runtime counters are process-local; the
current production baseline runs one Uvicorn process per API container, and
Prometheus must scrape every API replica when the service is horizontally scaled.

Core metrics:

- `crawler_webhook_outbox_pending`
- `crawler_webhook_outbox_oldest_pending_age_seconds`
- `crawler_webhook_outbox_credential_blocked`
- `crawler_webhook_outbox_permanently_failed`
- `crawler_webhook_outbox_retry_exhausted`
- `crawler_webhook_outbox_active_claims`
- `crawler_webhook_delivery_total{result=...}`
- `crawler_webhook_delivery_duration_seconds{result=...}`
- `crawler_webhook_claim_recovered_total`
- `crawler_webhook_stale_settlement_total`
- `crawler_webhook_retry_exhausted_total`
- `crawler_webhook_dispatcher_error_total`

Load `webhook-alerts.yml` into Prometheus-compatible rule evaluation. Thresholds
are deployment defaults and must be calibrated against the expected Job volume and
callback service-level objective.

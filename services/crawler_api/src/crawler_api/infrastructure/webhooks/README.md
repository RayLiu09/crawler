# Webhook delivery

Webhook delivery consumes the PostgreSQL Outbox, signs each event, applies
bounded exponential backoff, and records delivery without blocking job state
transactions.


# Crawler API tests

Unit tests cover state transitions and strict versioned event payloads.
PostgreSQL integration tests cover task claims, fencing tokens, idempotency,
record/event cursor pagination, transactional events, the Outbox schema, and
fault-injected Callback delivery over a real loopback HTTP connection.

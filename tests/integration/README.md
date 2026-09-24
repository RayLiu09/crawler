# Integration tests

Run against an isolated external PostgreSQL database with migrations applied.

Database tests are opt-in through `RUN_POSTGRES_INTEGRATION=1`. They insert callers,
Agents, jobs, tasks, Profiles, Sessions, Tokens, leases, attempts, parsed records,
idempotent batch receipts, versioned collection events, encrypted per-Job callback
credentials, and transactional event-to-Outbox rows. They verify application and
repository behavior through committed UoW savepoints inside outer transactions,
then roll everything back. Every test confirms its stable identities are absent
afterward. Always point them at a migrated non-production database.

The Callback E2E suite also opens a loopback TCP server and runs the complete
PostgreSQL Outbox -> Dispatcher -> httpx -> Callback path. It injects HTTP 503,
disconnects, read timeouts, redirects, credential rotation, expired claim takeover,
and stale settlement races. The suite requires both database access and permission
to bind a local ephemeral port.

The Fake Source Adapter E2E suite uses a transaction-bound FastAPI application and
real HTTP serialization for caller Token issuance, Job submission, Agent Session,
heartbeat, claim, record batches, checkpoints, completion, Job aggregation, and
record keyset pagination. Its Agent actor validates the protocol boundary but does
not stand in for the production desktop Agent runtime.

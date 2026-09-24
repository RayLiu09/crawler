# Alembic migrations

Migration scripts live in `versions/`. `0001_initial_schema.py` is the version 0.4
baseline and includes caller credentials, global Agent identities, task attempts,
parsed records, events, and the webhook Outbox. Deployment runs `alembic upgrade
head` before replacing the API container; application startup never mutates the
schema.

`0002_task_profile_leases.py` adds the active task-to-Browser-Profile binding and
a partial unique index that prevents one Profile from backing two active leases.

`0003_parsed_record_batches.py` adds idempotent parsed-record batch receipts and
stable server-receipt-order indexes. `0004_job_callback_configs.py` adds the
one-to-one Job callback configuration with encrypted Token material and strict
credential constraints. `0005_webhook_outbox_callback_configs.py` removes the
duplicated Outbox target URL and references the Job callback configuration instead.
`0006_callback_token_rotations.py` adds permanent rotation idempotency history and
explicit delivery states. `0007_webhook_dispatch_claims.py` adds recoverable,
short-lived dispatch ownership for concurrent Dispatcher instances.
`0008_callback_credential_destruction.py` adds a constrained destroyed state for
ciphertext and nonce, while retaining the non-secret fingerprint and rotation
history for deterministic replay and audit.
`0009_webhook_controlled_replays.py` adds the permanent idempotency and audit
history for operator-triggered retry-exhausted delivery replays. It separates
the current delivery generation's attempt budget from the cumulative replay
count and adds the status/creation keyset-query index.

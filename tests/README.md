# Cross-component tests

- `contract/`: OpenAPI and Pydantic compatibility between API and Agent.
- `integration/`: PostgreSQL leases, fencing tokens, idempotent record writes, and Outbox.
- `end_to_end/`: API plus a fake source Agent, followed by controlled real-site acceptance.


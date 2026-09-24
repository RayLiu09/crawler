# Control API transport

`HttpAgentTransport` uses an outbound-only `httpx.AsyncClient` with redirects
disabled. It implements session exchange, heartbeat, claim, lease heartbeat,
checkpoint, parsed-record upload, completion, and release operations. Session and
lease tokens stay in memory and are never written to the local spool or logs.

HTTP status handling is deliberately narrow: `204` means no claim or successful
empty operation; unexpected statuses and network failures become a stable
`AgentTransportError` for the Supervisor to retry, release, or reconnect.

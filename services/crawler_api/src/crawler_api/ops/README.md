# Trusted Agent provisioning

This package contains non-HTTP utilities used by the controlled installation
pipeline. Before an Agent can request a session, an authorized operator or
installation pipeline runs the control-side provisioning command. It generates a
unique `agentId` and high-entropy `deviceSecret`, inserts `collection_agents` with
the display name, versioned initial capabilities, `offline` status, and keyed
device credential hash in a database transaction, and emits the raw credential
exactly once to the controlled installer. The initial capabilities are
`{"schemaVersion":1,"supportedSources":[],"maxConcurrentTasks":1,"browser":
{"name":"chrome","headful":true}}`; authenticated heartbeats replace them with
the machine's observed values. PostgreSQL stores only the keyed credential hash.

The installed control-center image exposes the command as
`provision-agent --display-name "Desktop Agent A"` (the
`crawler-api-provision-agent` spelling is also available as an explicit alias).
It emits one
JSON document to stdout containing `agentId` and the one-time `deviceSecret`.
The installer must deliver that document through its controlled secret channel
and must not copy it into ordinary logs. An installation orchestrator may pass
`--agent-id <UUID>` and reuse that UUID on retries; an existing UUID is rejected
without changing the existing Agent or issuing a replacement secret.

This bootstrap command authenticates with deployment/operations authority and a
database connection; it does not need an Agent Session. The installed Agent then
uses its pre-provisioned device credential with `POST /v1/agent/sessions`. Only
after successful device verification does the API insert `agent_sessions` and
return a short-lived Agent Token. The session endpoint never creates an Agent.

Agents are global task executors. Provisioning must not assign a tenant, create an
API client, or expose tenant metadata to the Agent. Raw tenant keys, device
secrets, and access tokens must never be written to PostgreSQL or logs.

Credential lifecycle commands use the same control-side database authority:

- `rotate-agent-credential --agent-id <UUID>` locks the Agent row, replaces the
  device credential hash, revokes all unexpired Agent Sessions in the same
  transaction, and emits the new deviceSecret once.
- `disable-agent --agent-id <UUID>` locks the Agent row, sets its status to
  `disabled`, and revokes all unexpired Agent Sessions in the same transaction.
  Repeating the command is safe and remains disabled.

Rotation does not enable a disabled Agent. Neither command changes task ownership
or tenant data; current task recovery remains the lease coordinator's concern.

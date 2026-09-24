# Desktop Agent Runtime

The runtime is intentionally single-task per execution node in 0.4. The
Supervisor exchanges an Agent Session, publishes capabilities and Profile
status, claims one task, renews its lease, runs the source Adapter, uploads
validated record batches, confirms page checkpoints, and settles the task.

Runtime boundaries:

- `HttpAgentTransport` is outbound-only and keeps Session/lease credentials in
  memory. Redirects are disabled and transient network/5xx responses use a
  bounded retry policy.
- `LocalSpool` is a WAL/FULL SQLite queue for unacknowledged ParsedJobRecord
  batches and local checkpoints. It never stores HTML, cookies, screenshots,
  Profile content, tenant metadata, or tokens.
- `ProfileLock` prevents two local processes from opening the same source
  Profile. The lock marker contains local PID/host data and PID/create-time
  identities for the WebDriver-owned process tree, never a Profile path.
- `BrowserProcessSupervisor` gives WebDriver a bounded graceful shutdown and
  uses a PID-reuse-safe process tree fallback. The Profile lock remains held
  until the close path proves that the captured processes have exited.
- `AgentSupervisor` keeps Adapter collection in a worker thread while the
  async loop handles uploads, heartbeats, lease renewal and control responses.
  A page checkpoint is acknowledged by the Adapter only after records have
  been accepted by the API and the server checkpoint has succeeded.
- Control-plane retry exhaustion does not terminate the claim or heartbeat
  loops. The next poll/heartbeat retries the operation; a 401 causes a new
  in-memory Agent Session to be issued.
- `SIGINT`/`SIGTERM` stop new claims and request cancellation of the active
  Adapter at its next safe point before resources are released.

The local runtime is covered by focused tests. Real browser Profile restart,
two physical nodes, network interruption and crash recovery remain controlled
acceptance scenarios in `docs/acceptance-8.4.md`.

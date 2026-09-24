# Browser runtime

The Supervisor runs one source Adapter at a time per Agent. A source/profile
pair is protected by an atomic local lock under `AGENT_DATA_DIR/profile-locks`;
the lock owner records its local PID/host marker plus the WebDriver process
identities it owns. A process identity is PID plus creation time, which avoids
terminating an unrelated process after PID reuse. These local identities are
never uploaded to the control API. The persistent Profile remains under
`BROWSER_PROFILE_ROOT` and is never uploaded.

The current runtime starts the browser through the Adapter factory with the
configured visible Chrome/Edge Profile. `BrowserProcessSupervisor` snapshots
the WebDriver service and its descendants through `psutil`. The Adapter first
uses WebDriver `quit()`; if it raises, times out, or leaves owned processes
alive, the supervisor terminates and then kills only the captured process tree.
The Profile lock is released only after this close path returns. If an Agent
dies, the next lock holder may recover the exact recorded tree only after the
old Agent PID is confirmed dead. If fallback termination cannot prove that the
tree exited, the lock is deliberately retained and the Profile is reported as
`unavailable`; an operator restart/recovery must resolve it before reuse.

`SIGINT` and `SIGTERM` request cooperative cancellation at Adapter safe points;
the Agent stops claiming before settling the active task. Real Chrome/Edge
behavior on supported Windows/Linux execution machines, service installation,
Profile restart reuse, and two-node acceptance remain controlled checks in
`docs/acceptance-8.4.md`.

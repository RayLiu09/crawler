# Desktop Agent tests

`test_fake_source_adapter.py` verifies deterministic record generation, canonical
payload hashes, page checkpoints, control safe points, validation, and the absence
of browser dependencies. The Fake Adapter implements the `zhaopin` contract only
inside tests; it is not a public source code or a production website Adapter.

The PostgreSQL opt-in suite in `tests/integration/test_fake_source_adapter_e2e.py`
drives the real caller and Agent HTTP routes from token issuance through result
pagination. It uses an Agent test actor, not the production desktop Runtime running
on an execution machine. The local Supervisor, transport, and SQLite
spool have focused unit coverage. Browser process tests cover clean shutdown,
WebDriver failure/timeout fallback, stale Profile-lock recovery, and an actual
temporary child process terminated through the production `psutil` backend.
They do not substitute for Chrome/Edge process-tree evidence on each supported
execution-machine OS. Real recruitment site and multi-machine
tests remain controlled acceptance checks and are not part of unattended CI.
The controlled 8.4 acceptance sequence and evidence requirements are documented
in [`docs/acceptance-8.4.md`](../../../docs/acceptance-8.4.md); it must not be
replaced by the API test Actor.

`test_zhaopin_source_adapter.py` uses an in-memory Selenium-compatible DOM driver
to verify migrated selectors, list/detail precedence, URL/domain controls,
checkpoint safe points, browser cleanup, partial-detail fallback, Profile path
validation, `ParsedJobRecord` integrity, login/security-verification detection,
headless stable failures, same-driver visible-window continuation, bounded timeout,
state callbacks, and control cancellation during human waiting. It is not evidence
that the current live Zhaopin pages, real manual verification workflow, Profile
restart reuse, or two-machine acceptance have passed.

Both source adapters use the same control granularity: check before a page and
before starting the next record. A control request observed after a detail
navigation does not discard that already-started record; it stops at the next
safe point. Page checkpoints remain authoritative only after the runtime has
accepted all records yielded for the page.

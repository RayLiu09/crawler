# Zhaopin Source Adapter

Version 0.5.0 retains `locationText` independently from the detailed
`addressText`, captures `hiringText` and an explicit single `hiringCount`, and
keeps `publishTimeText` with a normalized `publishedDate`. `publishedAt` is
set only when the source supplies an explicit clock time. Relative dates use
Beijing time and the same observation timestamp as `collectedAt`; minute/hour
expressions yield inferred dates, never exact timestamps. Updates, refreshes
and recruiter reply activity are not publication facts. Missing/range/若干
headcounts have no numeric value.

The split summary matches icon filename prefixes instead of tag positions,
with text/label fallbacks. List location spans are combined. Where the detail
exposes a job ID, card selection waits for that ID (including identical job
titles); otherwise the modern summary title is checked. Legacy layouts without
either marker retain their existing readiness behavior.

Absent 0.5 fields are omitted from serialized records to preserve 0.4 record
hashes, batch receipts and local spool replays. Upgrade the API before agents;
see `deploy/agent/windows/UPGRADE-0.5.0.md` for deployment and rollback limits.

`ZhaopinSourceAdapter` implements the shared synchronous `SourceAdapter` contract.
It owns Zhaopin search URL construction, Selenium selectors, list/detail field
mapping, source URL validation, deterministic payload hashes, browser cleanup,
and page/record control safe points. Control is checked before each page and
before starting the next detail; once a detail has started, its current record
is completed and yielded. It does not import the control API,
repositories, upstream business models, or the legacy platform client.

Every parsed result returned by a source page is yielded, including a result
whose URL also appeared on an earlier page in the same collection. The adapter
does not use an in-memory cross-page duplicate set to decide whether to stop:
pagination ends only at the requested page limit or when the source returns no
listing cards. Record idempotency belongs to the receiving store, so retries or
repeated executions can be deduplicated there without hiding later pages.

The adapter detects the migrated login signals (`passport.zhaopin.com`, a
`/login` path, or the known login prompt) and security-verification signals
(`Security Verification`, `TEOCaptchaWidget`, or `正在验证连接安全性`). Headless
execution fails immediately with a stable adapter error. In visible mode the
same WebDriver and the same persistent Profile remain open while a local Agent
operator completes the step; no second browser, Profile, Cookie, page source,
or screenshot is created or uploaded. A narrow optional reporter receives only
`login_required`, `verification_required`, and `resumed` plus the current URL.
The wait has a bounded timeout and checks the collection control on every poll,
so pause/cancel can terminate it.

The default city-code registry is `city_codes.json` in this package. Each entry
maps a city or province name to its Zhaopin `jl` code. Province entries may use
their full names; the loader also registers the common short form, so `云南`
and `云南省` both use the bundled code `554`. An empty region omits `jl` and searches
nationwide. The loader rejects malformed JSON, duplicate names or codes, and
non-numeric codes rather than guessing.

To maintain the mapping outside the installed package, copy `city_codes.json`
to a persistent location and set `ZHAOPIN_CITY_CODES_FILE` in `agent.env` to its
absolute path. The external file replaces the bundled mapping, so keep all
required cities in it. Run
`desktop-collection-agent-check-config --config <absolute-agent.env>` after editing,
then restart the Agent. The file is read
when an adapter is created; a restart also refreshes any cached adapter.
The file name is configurable: a file named `cite_codex.json` is used only when
`ZHAOPIN_CITY_CODES_FILE` points to it.

The browser factory uses a persistent local Chrome or Edge user-data directory.
Job links are normalized to HTTPS and must belong to `zhaopin.com`; userinfo,
external domains, fragments, and non-standard ports are rejected before browser
navigation. Raw HTML, screenshots, cookies, and Profile content are never emitted
as parsed records.

Current automated tests use a deterministic DOM driver and do not contact the
live website. Login/security-verification detection and visible-window human
continuation are implemented and covered offline in task 8.3. Live selector
compatibility, real manual login/verification, Profile restart reuse, and
two-machine acceptance remain task 8.4.

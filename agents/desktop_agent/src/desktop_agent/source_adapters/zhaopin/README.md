# Zhaopin Source Adapter

`ZhaopinSourceAdapter` implements the shared synchronous `SourceAdapter` contract.
It owns Zhaopin search URL construction, Selenium selectors, list/detail field
mapping, source URL validation, deterministic payload hashes, browser cleanup,
and page/record control safe points. Control is checked before each page and
before starting the next detail; once a detail has started, its current record
is completed and yielded. It does not import the control API,
repositories, upstream business models, or the legacy platform client.

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

The default city-code registry covers the reviewed core first- and second-tier
city mappings used by the collector, including both `西安` and `西安市` (`854`).
An empty region deliberately omits `jl` and searches nationwide. Additional
regions must be supplied from a reviewed mapping and covered by tests; the
Adapter does not guess codes.

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

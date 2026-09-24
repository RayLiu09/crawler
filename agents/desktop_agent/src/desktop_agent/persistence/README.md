# Local spool

`LocalSpool` stores only validated `ParsedJobRecord` batches that have not yet
been acknowledged, plus the latest local task checkpoint. SQLite uses WAL and
`synchronous=FULL`; duplicate batch IDs are idempotent. Acknowledged rows and
terminal task checkpoints are deleted after task settlement. The spool never
stores HTML, screenshots, cookies, Profile content, tenant metadata, or tokens.

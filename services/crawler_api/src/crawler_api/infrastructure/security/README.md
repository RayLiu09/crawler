# Security adapters

This package owns tenant allowlist validation, API-client authentication,
controlled-install Agent device authentication, short-lived tokens, lease-token
hashing, and Callback Token encryption/fingerprinting. Agent identities and
API-client identities are separate security domains.

`tenant_allowlist.py` reads the mounted YAML Secret with a duplicate-key-rejecting
safe loader and validates it before startup continues. The application sync use
case treats that file as the authorization source: it upserts configured clients,
disables removed clients, and revokes their active access tokens in one Unit of
Work. Raw tenant keys are only accepted through `SecretStr` and are never stored.

Agent device credentials use `AGENT_DEVICE_CREDENTIAL_HASH_KEY`; short-lived
Agent Session Tokens use the independent `AGENT_TOKEN_HASH_KEY`. Both are stored
as HMAC-SHA256 digests. Session issuance, credential rotation, and disable acquire
a row lock on the Agent so an old device credential cannot race a rotation and
leave behind a newly issued, unrevoked Session.

Task lease Tokens use deterministic HMAC issuance so the same `claimRequestId`
can recover an identical active lease after an HTTP response is lost. Issuance and
storage keys are derived from `AGENT_TOKEN_HASH_KEY` with distinct fixed labels;
the database stores only the storage-domain HMAC digest. The derivation keeps the
lease and Session Token domains separate without adding another deployed Secret.

Per-Job Callback Tokens use `CALLBACK_TOKEN_ENCRYPTION_KEY` for AES-256-GCM and
an independent `CALLBACK_TOKEN_FINGERPRINT_KEY` for idempotency comparison. The
database stores ciphertext, a random 96-bit nonce, HMAC-SHA256 fingerprint, expiry,
credential version, and encryption-key version; it never stores the plaintext.

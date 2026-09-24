from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "crawler"
UUID_TYPE = PostgreSQLUUID(as_uuid=True)
TIMESTAMP = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class ApiClientRow(Base):
    __tablename__ = "api_clients"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(200))
    allowed_scopes: Mapped[list[str]] = mapped_column(
        JSONB,
        server_default=text(
            '\'["collection:write", "collection:read", "collection:control"]\'::jsonb'
        ),
    )
    config_fingerprint: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20))
    last_token_issued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class ApiAccessTokenRow(Base):
    __tablename__ = "api_access_tokens"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    client_id: Mapped[UUID] = mapped_column(ForeignKey("crawler.api_clients.id"))
    token_hash: Mapped[str] = mapped_column(String(128))
    issued_via_key_fingerprint: Mapped[str] = mapped_column(String(80))
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class CollectionAgentRow(Base):
    __tablename__ = "collection_agents"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    device_credential_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(200))
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        server_default=text(
            '\'{"schemaVersion": 1, "supportedSources": [], '
            '"maxConcurrentTasks": 1, "browser": {"name": "chrome", '
            '"headful": true}}\'::jsonb'
        ),
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    current_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("crawler.collection_tasks.id", ondelete="SET NULL")
    )


class AgentSessionRow(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_agents.id", ondelete="CASCADE")
    )
    session_token_hash: Mapped[str] = mapped_column(String(128))
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class BrowserProfileRow(Base):
    __tablename__ = "browser_profiles"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_agents.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String(60))
    profile_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class CollectionJobRow(Base):
    __tablename__ = "collection_jobs"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    client_id: Mapped[UUID] = mapped_column(ForeignKey("crawler.api_clients.id"))
    external_request_id: Mapped[str] = mapped_column(String(160))
    idempotency_key_hash: Mapped[str] = mapped_column(String(128))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    desired_state: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class CollectionTaskRow(Base):
    __tablename__ = "collection_tasks"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    collection_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_jobs.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String(60))
    keyword: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))
    page_limit: Mapped[int] = mapped_column(Integer)
    page_checkpoint: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    desired_state: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))
    leased_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("crawler.collection_agents.id"))
    leased_profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("crawler.browser_profiles.id", ondelete="SET NULL")
    )
    lease_token_hash: Mapped[str | None] = mapped_column(String(128))
    fencing_token: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    processed_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class CollectionTaskAttemptRow(Base):
    __tablename__ = "collection_task_attempts"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    collection_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_tasks.id", ondelete="CASCADE")
    )
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("crawler.collection_agents.id"))
    fencing_token: Mapped[int] = mapped_column(BigInteger)
    claimed_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    released_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    outcome: Mapped[str | None] = mapped_column(String(30))
    failure_code: Mapped[str | None] = mapped_column(String(100))


class ParsedJobRecordRow(Base):
    __tablename__ = "parsed_job_records"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    collection_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_tasks.id", ondelete="CASCADE")
    )
    source_job_id: Mapped[str | None] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(Text)
    query_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parsed_fields: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(80))
    collected_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class ParsedRecordBatchRow(Base):
    __tablename__ = "parsed_record_batches"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    collection_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_tasks.id", ondelete="CASCADE")
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_task_attempts.id", ondelete="CASCADE")
    )
    request_hash: Mapped[str] = mapped_column(String(80))
    record_ids: Mapped[list[str]] = mapped_column(JSONB)
    inserted_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class CollectionEventRow(Base):
    __tablename__ = "collection_events"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    collection_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_jobs.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class CollectionJobCallbackConfigRow(Base):
    __tablename__ = "collection_job_callback_configs"
    __table_args__ = (
        UniqueConstraint("collection_job_id", name="uq_collection_job_callback_configs_job"),
        CheckConstraint(
            "auth_scheme = 'bearer'",
            name="ck_collection_job_callback_configs_auth_scheme",
        ),
        CheckConstraint(
            "octet_length(token_ciphertext) >= 16",
            name="ck_collection_job_callback_configs_ciphertext_length",
        ),
        CheckConstraint(
            "octet_length(token_nonce) = 12",
            name="ck_collection_job_callback_configs_nonce_length",
        ),
        CheckConstraint(
            "(credential_destroyed_at IS NULL AND token_ciphertext IS NOT NULL "
            "AND token_nonce IS NOT NULL) OR "
            "(credential_destroyed_at IS NOT NULL AND token_ciphertext IS NULL "
            "AND token_nonce IS NULL)",
            name="ck_collection_job_callback_configs_credential_material",
        ),
        CheckConstraint(
            "token_fingerprint ~ '^hmac-sha256:[0-9a-f]{64}$'",
            name="ck_collection_job_callback_configs_fingerprint",
        ),
        CheckConstraint(
            "credential_version >= 1",
            name="ck_collection_job_callback_configs_credential_version",
        ),
        CheckConstraint(
            "encryption_key_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,39}$'",
            name="ck_collection_job_callback_configs_key_version",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    collection_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_jobs.id", ondelete="CASCADE")
    )
    target_url: Mapped[str] = mapped_column(Text)
    auth_scheme: Mapped[str] = mapped_column(String(20))
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_fingerprint: Mapped[str] = mapped_column(String(128))
    token_expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    credential_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    encryption_key_version: Mapped[str] = mapped_column(String(40))
    credential_destroyed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class CollectionJobCallbackCredentialRotationRow(Base):
    __tablename__ = "collection_job_callback_credential_rotations"
    __table_args__ = (
        UniqueConstraint(
            "callback_config_id",
            "idempotency_key_hash",
            name="uq_callback_credential_rotations_config_key",
        ),
        CheckConstraint(
            "idempotency_key_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_callback_credential_rotations_key_hash",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_callback_credential_rotations_request_fingerprint",
        ),
        CheckConstraint(
            "result_credential_version >= 2",
            name="ck_callback_credential_rotations_result_version",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    callback_config_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "crawler.collection_job_callback_configs.id",
            ondelete="CASCADE",
        )
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(128))
    result_credential_version: Mapped[int] = mapped_column(Integer)
    token_expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class WebhookOutboxRow(Base):
    __tablename__ = "webhook_outbox"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "callback_config_id",
            name="uq_webhook_outbox_event_callback",
        ),
        CheckConstraint(
            "status IN ('pending', 'credential_blocked', 'delivered', 'permanently_failed')",
            name="ck_webhook_outbox_status",
        ),
        CheckConstraint(
            "(dispatch_claim_id IS NULL AND dispatch_claimed_at IS NULL "
            "AND dispatch_lease_expires_at IS NULL) OR "
            "(dispatch_claim_id IS NOT NULL AND dispatch_claimed_at IS NOT NULL "
            "AND dispatch_lease_expires_at IS NOT NULL "
            "AND dispatch_lease_expires_at > dispatch_claimed_at)",
            name="ck_webhook_outbox_dispatch_claim",
        ),
        CheckConstraint("replay_count >= 0", name="ck_webhook_outbox_replay_count"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.collection_events.id", ondelete="CASCADE")
    )
    callback_config_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "crawler.collection_job_callback_configs.id",
            ondelete="CASCADE",
            name="fk_webhook_outbox_callback_config",
        )
    )
    status: Mapped[str] = mapped_column(String(30), server_default=text("'pending'"))
    dispatch_claim_id: Mapped[UUID | None] = mapped_column(UUID_TYPE)
    dispatch_claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    dispatch_lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    replay_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_replayed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    next_attempt_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    last_failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())


class WebhookOutboxReplayRow(Base):
    __tablename__ = "webhook_outbox_replays"
    __table_args__ = (
        UniqueConstraint(
            "webhook_outbox_id",
            "idempotency_key_hash",
            name="uq_webhook_outbox_replays_outbox_key",
        ),
        UniqueConstraint(
            "webhook_outbox_id",
            "replay_number",
            name="uq_webhook_outbox_replays_outbox_number",
        ),
        CheckConstraint(
            "idempotency_key_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_webhook_outbox_replays_key_hash",
        ),
        CheckConstraint(
            "replay_number >= 1",
            name="ck_webhook_outbox_replays_number",
        ),
        CheckConstraint(
            "previous_attempt_count >= 1",
            name="ck_webhook_outbox_replays_previous_attempt_count",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid4)
    webhook_outbox_id: Mapped[UUID] = mapped_column(
        ForeignKey("crawler.webhook_outbox.id", ondelete="CASCADE")
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(128))
    replay_number: Mapped[int] = mapped_column(Integer)
    previous_attempt_count: Mapped[int] = mapped_column(Integer)
    previous_failure_code: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())

"""Create the version 0.4 control-plane schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TZ = sa.DateTime(timezone=True)
ALL_API_SCOPES_DEFAULT = sa.text(
    '\'["collection:write", "collection:read", "collection:control"]\'::jsonb'
)
AGENT_CAPABILITIES_DEFAULT = sa.text(
    '\'{"schemaVersion": 1, "supportedSources": [], '
    '"maxConcurrentTasks": 1, "browser": {"name": "chrome", '
    '"headful": true}}\'::jsonb'
)


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema(SCHEMA))

    op.create_table(
        "api_clients",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("allowed_scopes", JSONB, nullable=False, server_default=ALL_API_SCOPES_DEFAULT),
        sa.Column("config_fingerprint", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("last_token_issued_at", TZ),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_api_clients_status"),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_scopes) = 'array' "
            "AND allowed_scopes <@ "
            '\'["collection:write", "collection:read", "collection:control"]\'::jsonb',
            name="ck_api_clients_allowed_scopes_array",
        ),
        sa.UniqueConstraint("tenant_id", name="uq_api_clients_tenant_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "api_access_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("client_id", UUID, sa.ForeignKey("crawler.api_clients.id"), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("issued_via_key_fingerprint", sa.String(80), nullable=False),
        sa.Column("scopes", JSONB, nullable=False),
        sa.Column("issued_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("last_used_at", TZ),
        sa.Column("revoked_at", TZ),
        sa.CheckConstraint(
            "jsonb_typeof(scopes) = 'array' "
            "AND scopes <@ "
            '\'["collection:write", "collection:read", "collection:control"]\'::jsonb',
            name="ck_api_access_tokens_scopes_array",
        ),
        sa.UniqueConstraint("token_hash", name="uq_api_access_tokens_token_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_api_access_tokens_client_expiry",
        "api_access_tokens",
        ["client_id", "expires_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "collection_agents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("device_credential_hash", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("capabilities", JSONB, nullable=False, server_default=AGENT_CAPABILITIES_DEFAULT),
        sa.Column("heartbeat_at", TZ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'disabled')", name="ck_collection_agents_status"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities) = 'object' "
            "AND capabilities ->> 'schemaVersion' = '1' "
            "AND jsonb_typeof(capabilities -> 'supportedSources') = 'array' "
            "AND jsonb_typeof(capabilities -> 'maxConcurrentTasks') = 'number' "
            "AND (capabilities ->> 'maxConcurrentTasks')::integer BETWEEN 1 AND 32 "
            "AND jsonb_typeof(capabilities -> 'browser') = 'object'",
            name="ck_collection_agents_capabilities_v1",
        ),
        sa.UniqueConstraint(
            "device_credential_hash", name="uq_collection_agents_device_credential_hash"
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "agent_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "agent_id",
            UUID,
            sa.ForeignKey("crawler.collection_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_token_hash", sa.String(128), nullable=False),
        sa.Column("issued_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("last_used_at", TZ),
        sa.Column("revoked_at", TZ),
        sa.UniqueConstraint("session_token_hash", name="uq_agent_sessions_token_hash"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_agent_sessions_agent_expiry",
        "agent_sessions",
        ["agent_id", "expires_at"],
        schema=SCHEMA,
    )
    op.create_table(
        "browser_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "agent_id",
            UUID,
            sa.ForeignKey("crawler.collection_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("profile_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('ready', 'busy', 'login_required', 'unavailable')",
            name="ck_browser_profiles_status",
        ),
        sa.CheckConstraint("source IN ('zhaopin')", name="ck_browser_profiles_source"),
        sa.CheckConstraint(
            "profile_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'",
            name="ck_browser_profiles_profile_key",
        ),
        sa.UniqueConstraint(
            "agent_id", "source", "profile_key", name="uq_browser_profiles_agent_source_key"
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "collection_jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("client_id", UUID, sa.ForeignKey("crawler.api_clients.id"), nullable=False),
        sa.Column("external_request_id", sa.String(160), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(128), nullable=False),
        sa.Column("request_payload", JSONB, nullable=False),
        sa.Column("desired_state", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TZ),
        sa.CheckConstraint(
            "external_request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$'",
            name="ck_collection_jobs_external_request_id",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request_payload) = 'object' "
            "AND request_payload ->> 'schemaVersion' = '1' "
            "AND request_payload ->> 'source' IN ('zhaopin') "
            "AND jsonb_typeof(request_payload -> 'queries') = 'array' "
            "AND jsonb_typeof(request_payload -> 'metadata') = 'object'",
            name="ck_collection_jobs_request_payload_v1",
        ),
        sa.UniqueConstraint(
            "client_id", "external_request_id", name="uq_collection_jobs_client_external_request"
        ),
        sa.UniqueConstraint(
            "client_id", "idempotency_key_hash", name="uq_collection_jobs_client_idempotency"
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "collection_tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "collection_job_id",
            UUID,
            sa.ForeignKey("crawler.collection_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("keyword", sa.String(120), nullable=False),
        sa.Column("region", sa.String(80), nullable=False),
        sa.Column("page_limit", sa.Integer(), nullable=False),
        sa.Column("page_checkpoint", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("desired_state", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column(
            "leased_agent_id", UUID, sa.ForeignKey("crawler.collection_agents.id"), nullable=True
        ),
        sa.Column("lease_token_hash", sa.String(128)),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", TZ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(100)),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("page_limit > 0", name="ck_collection_tasks_page_limit"),
        sa.CheckConstraint("page_checkpoint >= 0", name="ck_collection_tasks_page_checkpoint"),
        sa.CheckConstraint("source IN ('zhaopin')", name="ck_collection_tasks_source"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_collection_tasks_status_lease_expiry",
        "collection_tasks",
        ["status", "lease_expires_at"],
        schema=SCHEMA,
    )
    op.add_column("collection_agents", sa.Column("current_task_id", UUID), schema=SCHEMA)
    op.create_foreign_key(
        "fk_collection_agents_current_task",
        "collection_agents",
        "collection_tasks",
        ["current_task_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.create_table(
        "collection_task_attempts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "collection_task_id",
            UUID,
            sa.ForeignKey("crawler.collection_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", UUID, sa.ForeignKey("crawler.collection_agents.id"), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("claimed_at", TZ, nullable=False),
        sa.Column("released_at", TZ),
        sa.Column("outcome", sa.String(30)),
        sa.Column("failure_code", sa.String(100)),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('completed', 'paused', 'cancelled', 'failed', 'lease_expired')",
            name="ck_collection_task_attempts_outcome",
        ),
        sa.UniqueConstraint(
            "collection_task_id",
            "fencing_token",
            name="uq_collection_task_attempts_task_fencing",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "parsed_job_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "collection_task_id",
            UUID,
            sa.ForeignKey("crawler.collection_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_job_id", sa.String(200)),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("query_context", JSONB, nullable=False),
        sa.Column("parsed_fields", JSONB, nullable=False),
        sa.Column("payload_hash", sa.String(80), nullable=False),
        sa.Column("collected_at", TZ, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "collection_task_id",
            "source_url",
            "payload_hash",
            name="uq_parsed_job_records_task_url_payload",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_parsed_job_records_task_collected_id",
        "parsed_job_records",
        ["collection_task_id", "collected_at", "id"],
        schema=SCHEMA,
    )
    op.create_table(
        "collection_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "collection_job_id",
            UUID,
            sa.ForeignKey("crawler.collection_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("occurred_at", TZ, nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_collection_events_job_occurred",
        "collection_events",
        ["collection_job_id", "occurred_at", "id"],
        schema=SCHEMA,
    )
    op.create_table(
        "webhook_outbox",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "event_id",
            UUID,
            sa.ForeignKey("crawler.collection_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", TZ, nullable=False),
        sa.Column("delivered_at", TZ),
        sa.Column("last_failure_code", sa.String(100)),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_id", "target_url", name="uq_webhook_outbox_event_target"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_webhook_outbox_pending",
        "webhook_outbox",
        ["delivered_at", "next_attempt_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("webhook_outbox", schema=SCHEMA)
    op.drop_table("collection_events", schema=SCHEMA)
    op.drop_table("parsed_job_records", schema=SCHEMA)
    op.drop_table("collection_task_attempts", schema=SCHEMA)
    op.drop_constraint(
        "fk_collection_agents_current_task", "collection_agents", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_column("collection_agents", "current_task_id", schema=SCHEMA)
    op.drop_table("collection_tasks", schema=SCHEMA)
    op.drop_table("collection_jobs", schema=SCHEMA)
    op.drop_table("browser_profiles", schema=SCHEMA)
    op.drop_table("agent_sessions", schema=SCHEMA)
    op.drop_table("collection_agents", schema=SCHEMA)
    op.drop_table("api_access_tokens", schema=SCHEMA)
    op.drop_table("api_clients", schema=SCHEMA)
    op.execute(sa.schema.DropSchema(SCHEMA))

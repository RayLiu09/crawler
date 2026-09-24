"""Add controlled webhook delivery replay history.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "webhook_outbox",
        sa.Column("replay_count", sa.Integer(), server_default="0", nullable=False),
        schema=SCHEMA,
    )
    op.add_column(
        "webhook_outbox",
        sa.Column("last_replayed_at", TZ, nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_webhook_outbox_replay_count",
        "webhook_outbox",
        "replay_count >= 0",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_webhook_outbox_status_created",
        "webhook_outbox",
        ["status", "created_at", "id"],
        schema=SCHEMA,
    )
    op.create_table(
        "webhook_outbox_replays",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "webhook_outbox_id",
            UUID,
            sa.ForeignKey("crawler.webhook_outbox.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key_hash", sa.String(128), nullable=False),
        sa.Column("replay_number", sa.Integer(), nullable=False),
        sa.Column("previous_attempt_count", sa.Integer(), nullable=False),
        sa.Column("previous_failure_code", sa.String(100), nullable=False),
        sa.Column("created_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_webhook_outbox_replays_key_hash",
        ),
        sa.CheckConstraint(
            "replay_number >= 1",
            name="ck_webhook_outbox_replays_number",
        ),
        sa.CheckConstraint(
            "previous_attempt_count >= 1",
            name="ck_webhook_outbox_replays_previous_attempt_count",
        ),
        sa.UniqueConstraint(
            "webhook_outbox_id",
            "idempotency_key_hash",
            name="uq_webhook_outbox_replays_outbox_key",
        ),
        sa.UniqueConstraint(
            "webhook_outbox_id",
            "replay_number",
            name="uq_webhook_outbox_replays_outbox_number",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("webhook_outbox_replays", schema=SCHEMA)
    op.drop_index(
        "ix_webhook_outbox_status_created",
        table_name="webhook_outbox",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_webhook_outbox_replay_count",
        "webhook_outbox",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("webhook_outbox", "last_replayed_at", schema=SCHEMA)
    op.drop_column("webhook_outbox", "replay_count", schema=SCHEMA)

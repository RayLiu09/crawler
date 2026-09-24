"""Add recoverable webhook dispatch claims.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "webhook_outbox",
        sa.Column("dispatch_claim_id", UUID),
        schema=SCHEMA,
    )
    op.add_column(
        "webhook_outbox",
        sa.Column("dispatch_claimed_at", TZ),
        schema=SCHEMA,
    )
    op.add_column(
        "webhook_outbox",
        sa.Column("dispatch_lease_expires_at", TZ),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_webhook_outbox_dispatch_claim",
        "webhook_outbox",
        "(dispatch_claim_id IS NULL AND dispatch_claimed_at IS NULL "
        "AND dispatch_lease_expires_at IS NULL) OR "
        "(dispatch_claim_id IS NOT NULL AND dispatch_claimed_at IS NOT NULL "
        "AND dispatch_lease_expires_at IS NOT NULL "
        "AND dispatch_lease_expires_at > dispatch_claimed_at)",
        schema=SCHEMA,
    )
    op.drop_index("ix_webhook_outbox_pending", table_name="webhook_outbox", schema=SCHEMA)
    op.create_index(
        "ix_webhook_outbox_pending",
        "webhook_outbox",
        ["status", "next_attempt_at", "dispatch_lease_expires_at", "id"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_outbox_pending", table_name="webhook_outbox", schema=SCHEMA)
    op.create_index(
        "ix_webhook_outbox_pending",
        "webhook_outbox",
        ["delivered_at", "next_attempt_at"],
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_webhook_outbox_dispatch_claim",
        "webhook_outbox",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("webhook_outbox", "dispatch_lease_expires_at", schema=SCHEMA)
    op.drop_column("webhook_outbox", "dispatch_claimed_at", schema=SCHEMA)
    op.drop_column("webhook_outbox", "dispatch_claim_id", schema=SCHEMA)

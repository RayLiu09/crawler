"""Add callback credential rotation history and Outbox delivery status.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "webhook_outbox",
        sa.Column("status", sa.String(30), server_default="pending", nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            "UPDATE crawler.webhook_outbox SET status = CASE "
            "WHEN delivered_at IS NULL THEN 'pending' ELSE 'delivered' END"
        )
    )
    op.alter_column(
        "webhook_outbox",
        "status",
        existing_type=sa.String(30),
        server_default="pending",
        nullable=False,
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_webhook_outbox_status",
        "webhook_outbox",
        "status IN ('pending', 'credential_blocked', 'delivered', 'permanently_failed')",
        schema=SCHEMA,
    )

    op.create_table(
        "collection_job_callback_credential_rotations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "callback_config_id",
            UUID,
            sa.ForeignKey(
                "crawler.collection_job_callback_configs.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key_hash", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        sa.Column("result_credential_version", sa.Integer(), nullable=False),
        sa.Column("token_expires_at", TZ, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "callback_config_id",
            "idempotency_key_hash",
            name="uq_callback_credential_rotations_config_key",
        ),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_callback_credential_rotations_key_hash",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_callback_credential_rotations_request_fingerprint",
        ),
        sa.CheckConstraint(
            "result_credential_version >= 2",
            name="ck_callback_credential_rotations_result_version",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table(
        "collection_job_callback_credential_rotations",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "ck_webhook_outbox_status",
        "webhook_outbox",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("webhook_outbox", "status", schema=SCHEMA)

"""Persist idempotent parsed-record batch receipts and cursor order.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "parsed_record_batches",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "collection_task_id",
            UUID,
            sa.ForeignKey("crawler.collection_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            UUID,
            sa.ForeignKey("crawler.collection_task_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(80), nullable=False),
        sa.Column("record_ids", JSONB, nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "request_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_parsed_record_batches_request_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(record_ids) = 'array'",
            name="ck_parsed_record_batches_record_ids_array",
        ),
        sa.CheckConstraint(
            "inserted_count >= 0 AND inserted_count <= jsonb_array_length(record_ids)",
            name="ck_parsed_record_batches_inserted_count",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_parsed_job_records_task_created_id",
        "parsed_job_records",
        ["collection_task_id", "created_at", "id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_parsed_job_records_task_created_id",
        table_name="parsed_job_records",
        schema=SCHEMA,
    )
    op.drop_table("parsed_record_batches", schema=SCHEMA)

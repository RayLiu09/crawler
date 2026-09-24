"""Add encrypted per-Job callback configuration.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
UUID = postgresql.UUID(as_uuid=True)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "collection_job_callback_configs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "collection_job_id",
            UUID,
            sa.ForeignKey("crawler.collection_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("auth_scheme", sa.String(20), nullable=False),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("token_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("token_fingerprint", sa.String(128), nullable=False),
        sa.Column("token_expires_at", TZ, nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("encryption_key_version", sa.String(40), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "auth_scheme = 'bearer'",
            name="ck_collection_job_callback_configs_auth_scheme",
        ),
        sa.CheckConstraint(
            "octet_length(token_ciphertext) >= 16",
            name="ck_collection_job_callback_configs_ciphertext_length",
        ),
        sa.CheckConstraint(
            "octet_length(token_nonce) = 12",
            name="ck_collection_job_callback_configs_nonce_length",
        ),
        sa.CheckConstraint(
            "token_fingerprint ~ '^hmac-sha256:[0-9a-f]{64}$'",
            name="ck_collection_job_callback_configs_fingerprint",
        ),
        sa.CheckConstraint(
            "credential_version >= 1",
            name="ck_collection_job_callback_configs_credential_version",
        ),
        sa.CheckConstraint(
            "encryption_key_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,39}$'",
            name="ck_collection_job_callback_configs_key_version",
        ),
        sa.UniqueConstraint(
            "collection_job_id", name="uq_collection_job_callback_configs_job"
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("collection_job_callback_configs", schema=SCHEMA)

"""Destroy callback credential material after final delivery.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "collection_job_callback_configs",
        sa.Column("credential_destroyed_at", TZ, nullable=True),
        schema=SCHEMA,
    )
    op.alter_column(
        "collection_job_callback_configs",
        "token_ciphertext",
        existing_type=sa.LargeBinary(),
        nullable=True,
        schema=SCHEMA,
    )
    op.alter_column(
        "collection_job_callback_configs",
        "token_nonce",
        existing_type=sa.LargeBinary(),
        nullable=True,
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_collection_job_callback_configs_credential_material",
        "collection_job_callback_configs",
        "(credential_destroyed_at IS NULL AND token_ciphertext IS NOT NULL "
        "AND token_nonce IS NOT NULL) OR "
        "(credential_destroyed_at IS NOT NULL AND token_ciphertext IS NULL "
        "AND token_nonce IS NULL)",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_collection_job_callback_configs_credential_material",
        "collection_job_callback_configs",
        schema=SCHEMA,
        type_="check",
    )
    op.execute(
        sa.text(
            "UPDATE crawler.collection_job_callback_configs "
            "SET token_ciphertext = decode(repeat('00', 16), 'hex'), "
            "token_nonce = decode(repeat('00', 12), 'hex') "
            "WHERE credential_destroyed_at IS NOT NULL"
        )
    )
    op.alter_column(
        "collection_job_callback_configs",
        "token_nonce",
        existing_type=sa.LargeBinary(),
        nullable=False,
        schema=SCHEMA,
    )
    op.alter_column(
        "collection_job_callback_configs",
        "token_ciphertext",
        existing_type=sa.LargeBinary(),
        nullable=False,
        schema=SCHEMA,
    )
    op.drop_column(
        "collection_job_callback_configs",
        "credential_destroyed_at",
        schema=SCHEMA,
    )

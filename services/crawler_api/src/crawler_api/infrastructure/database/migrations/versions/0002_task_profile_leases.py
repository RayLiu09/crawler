"""Persist the Browser Profile selected for an active task lease.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"


def upgrade() -> None:
    op.add_column(
        "collection_tasks",
        sa.Column("leased_profile_id", UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_collection_tasks_leased_profile",
        "collection_tasks",
        "browser_profiles",
        ["leased_profile_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_collection_tasks_active_profile",
        "collection_tasks",
        ["leased_profile_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("leased_profile_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_collection_tasks_active_profile",
        table_name="collection_tasks",
        schema=SCHEMA,
    )
    op.drop_constraint(
        "fk_collection_tasks_leased_profile",
        "collection_tasks",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("collection_tasks", "leased_profile_id", schema=SCHEMA)

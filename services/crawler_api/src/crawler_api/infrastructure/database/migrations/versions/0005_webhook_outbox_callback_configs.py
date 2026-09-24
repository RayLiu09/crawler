"""Reference Job callback configurations from the webhook Outbox.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "crawler"
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "webhook_outbox",
        sa.Column("callback_config_id", UUID, nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_webhook_outbox_callback_config",
        "webhook_outbox",
        "collection_job_callback_configs",
        ["callback_config_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="CASCADE",
    )
    op.execute(
        sa.text(
            "UPDATE crawler.webhook_outbox AS outbox "
            "SET callback_config_id = callback.id "
            "FROM crawler.collection_events AS event "
            "JOIN crawler.collection_job_callback_configs AS callback "
            "ON callback.collection_job_id = event.collection_job_id "
            "WHERE event.id = outbox.event_id "
            "AND callback.target_url = outbox.target_url"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS ("
            "SELECT 1 FROM crawler.webhook_outbox WHERE callback_config_id IS NULL"
            ") THEN "
            "RAISE EXCEPTION 'legacy webhook_outbox row cannot be mapped to a callback config'; "
            "END IF; "
            "END $$"
        )
    )
    op.alter_column(
        "webhook_outbox",
        "callback_config_id",
        existing_type=UUID,
        nullable=False,
        schema=SCHEMA,
    )
    op.drop_constraint(
        "uq_webhook_outbox_event_target",
        "webhook_outbox",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_column("webhook_outbox", "target_url", schema=SCHEMA)
    op.create_unique_constraint(
        "uq_webhook_outbox_event_callback",
        "webhook_outbox",
        ["event_id", "callback_config_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.add_column(
        "webhook_outbox",
        sa.Column("target_url", sa.Text(), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            "UPDATE crawler.webhook_outbox AS outbox "
            "SET target_url = callback.target_url "
            "FROM crawler.collection_job_callback_configs AS callback "
            "WHERE callback.id = outbox.callback_config_id"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM crawler.webhook_outbox WHERE target_url IS NULL) THEN "
            "RAISE EXCEPTION 'webhook_outbox row has no callback target URL'; "
            "END IF; "
            "END $$"
        )
    )
    op.alter_column(
        "webhook_outbox",
        "target_url",
        existing_type=sa.Text(),
        nullable=False,
        schema=SCHEMA,
    )
    op.drop_constraint(
        "uq_webhook_outbox_event_callback",
        "webhook_outbox",
        schema=SCHEMA,
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_webhook_outbox_event_target",
        "webhook_outbox",
        ["event_id", "target_url"],
        schema=SCHEMA,
    )
    op.drop_constraint(
        "fk_webhook_outbox_callback_config",
        "webhook_outbox",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_column("webhook_outbox", "callback_config_id", schema=SCHEMA)

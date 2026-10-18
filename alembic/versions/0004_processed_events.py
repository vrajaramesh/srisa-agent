"""Dedupe table for payment events from mybill-service

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("order_no", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("processed_events", schema=SCHEMA)

"""Daily AI analysis report of the day's chats and sales (one row per IST date)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.create_table(
        "daily_reports",
        sa.Column("report_date", sa.Date(), primary_key=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("analysis", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chats_analysed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("daily_reports", schema=SCHEMA)

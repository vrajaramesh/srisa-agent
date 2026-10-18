"""Raw webhook capture (Instagram payloads, Phase 9 step 0)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.create_table(
        "raw_webhooks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_raw_webhooks_channel_received", "raw_webhooks", ["channel", "received_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("raw_webhooks", schema=SCHEMA)

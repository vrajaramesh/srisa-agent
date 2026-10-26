"""Which firm a chat belongs to (srisa = retail, srihitha = wholesale); one WhatsApp number serves both

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("conversations", sa.Column("firm", sa.Text(), nullable=False, server_default="srisa"), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("conversations", "firm", schema=SCHEMA)

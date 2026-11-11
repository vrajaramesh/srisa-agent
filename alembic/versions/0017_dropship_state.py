"""WhatsApp dropship browsing: which forwarded collection she is looking at, the variant she wants to buy and
whether the owner confirmed it is available

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("conversations", sa.Column("dropship_state", postgresql.JSONB()), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("conversations", "dropship_state", schema=SCHEMA)

"""Staff can remove a chat from Customer chats; it comes back when the customer writes again

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("conversations", sa.Column("dismissed_at", sa.DateTime(timezone=True)), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("conversations", "dismissed_at", schema=SCHEMA)

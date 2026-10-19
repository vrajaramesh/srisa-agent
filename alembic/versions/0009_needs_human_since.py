"""Staff inbox: when a conversation was handed to the team

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("conversations", sa.Column("needs_human_since", sa.DateTime(timezone=True)), schema=SCHEMA)
    op.execute(f"UPDATE {SCHEMA}.conversations SET needs_human_since = updated_at WHERE needs_human")
    op.create_index("ix_conversations_waiting", "conversations", ["needs_human_since"],
                    postgresql_where=sa.text("needs_human"), schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_conversations_waiting", "conversations", schema=SCHEMA)
    op.drop_column("conversations", "needs_human_since", schema=SCHEMA)

"""Click-to-WhatsApp ad referrals: entry source on conversations + conversation_entries history

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]
RESOLUTIONS = "('ad_link', 'ad_text', 'message_text', 'none')"
COLUMNS = (
    ("entry_source_type", sa.Text()),
    ("entry_source_id", sa.Text()),
    ("entry_ctwa_clid", sa.Text()),
    ("entry_headline", sa.Text()),
    ("entry_label", sa.Text()),
    ("entry_resolution", sa.Text()),
    ("entry_at", sa.DateTime(timezone=True)),
    ("fep_expires_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("conversations", sa.Column(name, type_), schema=SCHEMA)
    op.create_check_constraint("ck_conversations_entry_resolution", "conversations",
                               f"entry_resolution IS NULL OR entry_resolution IN {RESOLUTIONS}", schema=SCHEMA)
    op.create_table(
        "conversation_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey(f"{SCHEMA}.messages.id", ondelete="SET NULL")),
        sa.Column("source_type", sa.Text()),
        sa.Column("source_id", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("headline", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("media_type", sa.Text()),
        sa.Column("ctwa_clid", sa.Text()),
        sa.Column("welcome_text", sa.Text()),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_conversation_entries_conversation", "conversation_entries",
                    ["conversation_id", "created_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("conversation_entries", schema=SCHEMA)
    op.drop_constraint("ck_conversations_entry_resolution", "conversations", schema=SCHEMA)
    for name, _ in reversed(COLUMNS):
        op.drop_column("conversations", name, schema=SCHEMA)

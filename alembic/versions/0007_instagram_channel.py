"""Instagram channel: echoes, offered choices, profile, message author

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("messages", sa.Column("author", sa.Text()), schema=SCHEMA)   # outbound: bot | staff
    op.create_check_constraint("ck_messages_author", "messages", "author IS NULL OR author IN ('bot', 'staff')",
                               schema=SCHEMA)
    op.add_column("conversations", sa.Column("profile_username", sa.Text()), schema=SCHEMA)
    op.add_column("conversations", sa.Column("profile_fetched_at", sa.DateTime(timezone=True)), schema=SCHEMA)
    # Last choices we offered ([{n, id, title}]) so a typed "2" maps back to a choice id.
    op.add_column("conversations", sa.Column("last_offered_choices", postgresql.JSONB()), schema=SCHEMA)
    op.drop_constraint("ck_conversations_entry_resolution", "conversations", schema=SCHEMA)
    op.create_check_constraint(
        "ck_conversations_entry_resolution", "conversations",
        "entry_resolution IS NULL OR entry_resolution IN ('ad_link', 'ad_text', 'message_text', 'ig_media_link', 'none')",
        schema=SCHEMA)
    op.create_table(
        "instagram_echoes",
        sa.Column("mid", sa.Text(), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision", sa.Text()),   # ours | staff
        schema=SCHEMA,
    )
    op.create_index("ix_instagram_echoes_pending", "instagram_echoes", ["conversation_id"],
                    postgresql_where=sa.text("decided_at IS NULL"), schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("instagram_echoes", schema=SCHEMA)
    op.drop_constraint("ck_conversations_entry_resolution", "conversations", schema=SCHEMA)
    op.create_check_constraint(
        "ck_conversations_entry_resolution", "conversations",
        "entry_resolution IS NULL OR entry_resolution IN ('ad_link', 'ad_text', 'message_text', 'none')", schema=SCHEMA)
    for col in ("last_offered_choices", "profile_fetched_at", "profile_username"):
        op.drop_column("conversations", col, schema=SCHEMA)
    op.drop_constraint("ck_messages_author", "messages", schema=SCHEMA)
    op.drop_column("messages", "author", schema=SCHEMA)

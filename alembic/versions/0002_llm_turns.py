"""AI agent: conversations.needs_human_reason, llm_turns

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("conversations", sa.Column("needs_human_reason", sa.Text()), schema=SCHEMA)
    op.create_table(
        "llm_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stop_reason", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_llm_turns_conversation_created", "llm_turns", ["conversation_id", "created_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_llm_turns_conversation_created", table_name="llm_turns", schema=SCHEMA)
    op.drop_table("llm_turns", schema=SCHEMA)
    op.drop_column("conversations", "needs_human_reason", schema=SCHEMA)

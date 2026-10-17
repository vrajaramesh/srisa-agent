"""agent schema: conversations, messages, product_links, ref_codes

Revision ID: 0001
Revises:
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Target schema comes from env.py (DB_SCHEMA): "agent" normally, "agent_test" in tests.
SCHEMA = op.get_context().opts["version_table_schema"]

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=False),
        sa.Column("customer_name", sa.Text()),
        sa.Column("stage", sa.Text(), nullable=False, server_default=sa.text("'new'")),
        sa.Column("current_product_code", sa.Text()),
        sa.Column("needs_human", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_customer_message_at", TZ),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("channel", "external_user_id", name="uq_conversations_channel_user"),
        sa.CheckConstraint("channel IN ('whatsapp', 'instagram')", name="ck_conversations_channel"),
        schema=SCHEMA,
    )
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("external_message_id", sa.Text(), unique=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column("processed_at", TZ),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_messages_direction"),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'failed', 'skipped', 'sent', 'delivered', 'read')",
            name="ck_messages_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_messages_conversation_status_created",
        "messages",
        ["conversation_id", "status", "created_at"],
        schema=SCHEMA,
    )
    op.create_table(
        "product_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False, unique=True),
        sa.Column("product_code", sa.Text(), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source_type IN ('ad', 'media')", name="ck_product_links_source_type"),
        schema=SCHEMA,
    )
    op.create_table(
        "ref_codes",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("ig_user_id", sa.Text(), nullable=False),
        sa.Column("wa_id", sa.Text()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("used_at", TZ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("ref_codes", schema=SCHEMA)
    op.drop_table("product_links", schema=SCHEMA)
    op.drop_index("ix_messages_conversation_status_created", table_name="messages", schema=SCHEMA)
    op.drop_table("messages", schema=SCHEMA)
    op.drop_table("conversations", schema=SCHEMA)

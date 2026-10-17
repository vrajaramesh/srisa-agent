"""Order flow: order_drafts, conversation stage values + mybill_customer_id, messages.reply_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]
STAGES = "('browsing', 'sizing', 'customise', 'address', 'confirm', 'order_created')"


def upgrade() -> None:
    op.execute(f"UPDATE {SCHEMA}.conversations SET stage = 'browsing' WHERE stage NOT IN {STAGES}")
    op.alter_column("conversations", "stage", server_default=sa.text("'browsing'"), schema=SCHEMA)
    op.create_check_constraint("ck_conversations_stage", "conversations", f"stage IN {STAGES}", schema=SCHEMA)
    op.add_column("conversations", sa.Column("mybill_customer_id", sa.Integer()), schema=SCHEMA)
    op.add_column("messages", sa.Column("reply_id", sa.Text()), schema=SCHEMA)

    n = sa.Numeric(5, 1)
    op.create_table(
        "order_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("design_code", sa.Text()),
        sa.Column("size_code", sa.Text()),
        sa.Column("height_cm", n),
        sa.Column("fit_preference", sa.Text()),
        sa.Column("bust_in", n),
        sa.Column("waist_in", n),
        sa.Column("hip_in", n),
        sa.Column("measurement_notes", sa.Text()),
        sa.Column("option_ids", postgresql.ARRAY(sa.Integer()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("qty", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("delivery_name", sa.Text()),
        sa.Column("delivery_phone", sa.Text()),
        sa.Column("address_line1", sa.Text()),
        sa.Column("address_line2", sa.Text()),
        sa.Column("city", sa.Text()),
        sa.Column("state", sa.Text()),
        sa.Column("pincode", sa.Text()),
        sa.Column("last_quote", postgresql.JSONB()),
        sa.Column("summary_sent_version", sa.Integer()),
        sa.Column("mybill_order_no", sa.Text()),
        sa.Column("mybill_customer_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('open', 'submitted', 'cancelled')", name="ck_order_drafts_status"),
        sa.CheckConstraint("qty BETWEEN 1 AND 5", name="ck_order_drafts_qty"),
        schema=SCHEMA,
    )
    # At most one open draft per conversation.
    op.create_index("uq_order_drafts_open_per_conversation", "order_drafts", ["conversation_id"], unique=True,
                    postgresql_where=sa.text("status = 'open'"), schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("uq_order_drafts_open_per_conversation", table_name="order_drafts", schema=SCHEMA)
    op.drop_table("order_drafts", schema=SCHEMA)
    op.drop_column("messages", "reply_id", schema=SCHEMA)
    op.drop_column("conversations", "mybill_customer_id", schema=SCHEMA)
    op.drop_constraint("ck_conversations_stage", "conversations", schema=SCHEMA)
    op.alter_column("conversations", "stage", server_default=sa.text("'new'"), schema=SCHEMA)

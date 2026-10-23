"""Goods carts (fabric metres / readymade sizes from collections) and the collection a chat is about

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    # [{"code": "MCS-1B" | null, "product_id": 6224, "quantity": 2.5, "size": "M" | null, ...display fields}]
    op.add_column("order_drafts", sa.Column("lines", postgresql.JSONB(), nullable=False, server_default="[]"), schema=SCHEMA)
    op.drop_constraint("ck_order_drafts_kind", "order_drafts", schema=SCHEMA)
    op.create_check_constraint("ck_order_drafts_kind", "order_drafts", "kind IN ('garment', 'fabric', 'goods')", schema=SCHEMA)
    op.add_column("conversations", sa.Column("current_collection_code", sa.Text()), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("conversations", "current_collection_code", schema=SCHEMA)
    op.drop_constraint("ck_order_drafts_kind", "order_drafts", schema=SCHEMA)
    op.create_check_constraint("ck_order_drafts_kind", "order_drafts", "kind IN ('garment', 'fabric')", schema=SCHEMA)
    op.drop_column("order_drafts", "lines", schema=SCHEMA)

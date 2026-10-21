"""Fabric-only orders: an order draft can be for metres of a fabric instead of a stitched design

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("order_drafts", sa.Column("kind", sa.Text(), nullable=False, server_default="garment"), schema=SCHEMA)
    op.add_column("order_drafts", sa.Column("fabric_code", sa.Text()), schema=SCHEMA)
    op.add_column("order_drafts", sa.Column("fabric_metres", sa.Numeric(6, 2)), schema=SCHEMA)
    op.create_check_constraint("ck_order_drafts_kind", "order_drafts", "kind IN ('garment', 'fabric')", schema=SCHEMA)


def downgrade() -> None:
    op.drop_constraint("ck_order_drafts_kind", "order_drafts", schema=SCHEMA)
    op.drop_column("order_drafts", "fabric_metres", schema=SCHEMA)
    op.drop_column("order_drafts", "fabric_code", schema=SCHEMA)
    op.drop_column("order_drafts", "kind", schema=SCHEMA)

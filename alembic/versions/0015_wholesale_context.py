"""Wholesale chat context (current wholesale product, last quotation) kept between turns for mybill's wholesale tools

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("conversations", sa.Column("wholesale_context", postgresql.JSONB()), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("conversations", "wholesale_context", schema=SCHEMA)

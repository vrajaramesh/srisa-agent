"""Instagram -> WhatsApp handoff: ref codes carry the design and expire; entry_resolution 'ig_handoff'

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.add_column("ref_codes", sa.Column("design_code", sa.Text()), schema=SCHEMA)
    op.add_column("ref_codes", sa.Column("expires_at", sa.DateTime(timezone=True)), schema=SCHEMA)
    op.create_index("ix_ref_codes_ig_user", "ref_codes", ["ig_user_id", "created_at"], schema=SCHEMA)
    op.drop_constraint("ck_conversations_entry_resolution", "conversations", schema=SCHEMA)
    op.create_check_constraint(
        "ck_conversations_entry_resolution", "conversations",
        "entry_resolution IS NULL OR entry_resolution IN "
        "('ad_link', 'ad_text', 'message_text', 'ig_media_link', 'ig_handoff', 'none')", schema=SCHEMA)


def downgrade() -> None:
    op.drop_constraint("ck_conversations_entry_resolution", "conversations", schema=SCHEMA)
    op.create_check_constraint(
        "ck_conversations_entry_resolution", "conversations",
        "entry_resolution IS NULL OR entry_resolution IN ('ad_link', 'ad_text', 'message_text', 'ig_media_link', 'none')",
        schema=SCHEMA)
    op.drop_index("ix_ref_codes_ig_user", "ref_codes", schema=SCHEMA)
    op.drop_column("ref_codes", "expires_at", schema=SCHEMA)
    op.drop_column("ref_codes", "design_code", schema=SCHEMA)

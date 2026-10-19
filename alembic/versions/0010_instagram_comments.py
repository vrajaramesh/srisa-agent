"""Instagram comments: auto-reply bookkeeping (private DM with the WhatsApp order link + public reply)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = op.get_context().opts["version_table_schema"]


def upgrade() -> None:
    op.create_table(
        "instagram_comments",
        sa.Column("comment_id", sa.Text(), primary_key=True),
        sa.Column("media_id", sa.Text()),
        sa.Column("from_id", sa.Text()),
        sa.Column("username", sa.Text()),
        sa.Column("text", sa.Text()),
        sa.Column("raw", postgresql.JSONB()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # replied | skipped_own | skipped_no_intent | skipped_recent | skipped_not_allowed | failed
        sa.Column("outcome", sa.Text()),
        sa.Column("design_code", sa.Text()),
        sa.Column("private_reply_mid", sa.Text()),
        sa.Column("public_reply_id", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )
    op.create_index("ix_instagram_comments_user_media", "instagram_comments", ["from_id", "media_id", "processed_at"],
                    schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("instagram_comments", schema=SCHEMA)

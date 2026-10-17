"""ORM models for the `agent` schema. Alembic migrations are the source of truth for DDL."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "agent"

INBOUND, OUTBOUND = "in", "out"

# Message.status values
RECEIVED, PROCESSED, FAILED, SKIPPED = "received", "processed", "failed", "skipped"
SENT, DELIVERED, READ = "sent", "delivered", "read"
MESSAGE_STATUSES = (RECEIVED, PROCESSED, FAILED, SKIPPED, SENT, DELIVERED, READ)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Conversation(Base):
    """One customer on one channel."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("channel", "external_user_id", name="uq_conversations_channel_user"),
        CheckConstraint("channel IN ('whatsapp', 'instagram')", name="ck_conversations_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel: Mapped[str] = mapped_column(Text)
    external_user_id: Mapped[str] = mapped_column(Text)  # wa_id or IG user id
    customer_name: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text, server_default=sql_text("'new'"))
    current_product_code: Mapped[str | None] = mapped_column(Text)
    needs_human: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    # Why the bot handed over (e.g. "ready_to_order", "llm_failure: timeout"). Cleared on resume.
    needs_human_reason: Mapped[str | None] = mapped_column(Text)
    last_customer_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    """An inbound customer message or an outbound reply."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("direction IN ('in', 'out')", name="ck_messages_direction"),
        CheckConstraint(
            "status IN ('received', 'processed', 'failed', 'skipped', 'sent', 'delivered', 'read')",
            name="ck_messages_status",
        ),
        Index("ix_messages_conversation_status_created", "conversation_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    direction: Mapped[str] = mapped_column(Text)
    # Meta's "wamid...". Unique => duplicate webhook deliveries are stored once.
    external_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    # clock_timestamp() (not now()) so rows inserted in one transaction keep their order.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductLink(Base):
    """Maps a Click-to-WhatsApp ad or IG media id to a product. (Empty for now.)"""

    __tablename__ = "product_links"
    __table_args__ = (CheckConstraint("source_type IN ('ad', 'media')", name="ck_product_links_source_type"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_type: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text, unique=True)
    product_code: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefCode(Base):
    """Short code linking an Instagram user to a WhatsApp chat. (Empty for now.)"""

    __tablename__ = "ref_codes"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    ig_user_id: Mapped[str] = mapped_column(Text)
    wa_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LlmTurn(Base):
    """One AI agent run (all model calls + tool calls for one batch of customer messages)."""

    __tablename__ = "llm_turns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    model: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    # [{"name", "input", "result", "duration_ms"}] - result is a short summary, never secrets.
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    latency_ms: Mapped[int] = mapped_column(Integer, server_default=sql_text("0"))
    stop_reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

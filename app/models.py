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
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    # browsing | sizing | customise | address | confirm | order_created - changed ONLY by code (agent/flow.py)
    stage: Mapped[str] = mapped_column(Text, server_default=sql_text("'browsing'"))
    current_product_code: Mapped[str | None] = mapped_column(Text)
    needs_human: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    # mybill-service customers.customer_id (set by start_order). Never shown to the model.
    mybill_customer_id: Mapped[int | None] = mapped_column(Integer)
    # Why the bot handed over (e.g. "ready_to_order", "llm_failure: timeout"). Cleared on resume.
    needs_human_reason: Mapped[str | None] = mapped_column(Text)
    last_customer_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Newest Click-to-WhatsApp ad / post referral (history in conversation_entries). source_type "ad" | "post".
    entry_source_type: Mapped[str | None] = mapped_column(Text)
    entry_source_id: Mapped[str | None] = mapped_column(Text)
    entry_ctwa_clid: Mapped[str | None] = mapped_column(Text)
    entry_headline: Mapped[str | None] = mapped_column(Text)
    entry_label: Mapped[str | None] = mapped_column(Text)  # mybill marketing link label, when the ad is mapped
    # How the entry's design was found: ad_link | ad_text | message_text | none (NULL = not resolved yet)
    entry_resolution: Mapped[str | None] = mapped_column(Text)
    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Free entry point window (ad entries only): our first reply + 72h. See app/agent/fep.py.
    fep_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Instagram: @username from the User Profile API (fetched once; profile_fetched_at marks the attempt).
    profile_username: Mapped[str | None] = mapped_column(Text)
    profile_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last choices offered ([{n, id, title}]): a typed "2" is mapped back to that choice's id.
    last_offered_choices: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConversationEntry(Base):
    """Every ad / post referral a conversation came in through (the conversation keeps only the newest)."""

    __tablename__ = "conversation_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"))
    source_type: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)
    ctwa_clid: Mapped[str | None] = mapped_column(Text)
    welcome_text: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    # id of a tapped reply button / list row (e.g. "size:M", "confirm:<draft>:<version>"); its title is in text
    reply_id: Mapped[str | None] = mapped_column(Text)
    # Outbound only: bot (sent by us) | staff (typed by the team in the Instagram app, seen as an echo)
    author: Mapped[str | None] = mapped_column(Text)
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
    """Instagram -> WhatsApp handoff: the "ref IG-XXXXX" in the pre-typed WhatsApp message links her Instagram
    account (ig_user_id) to her WhatsApp number (wa_id, set when she sends it) and carries the design."""

    __tablename__ = "ref_codes"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    ig_user_id: Mapped[str] = mapped_column(Text)
    design_code: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


DRAFT_OPEN, DRAFT_SUBMITTED, DRAFT_CANCELLED = "open", "submitted", "cancelled"


class OrderDraft(Base):
    """The order being assembled in a conversation. Code owns it; the model only fills it via tools."""

    __tablename__ = "order_drafts"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'submitted', 'cancelled')", name="ck_order_drafts_status"),
        CheckConstraint("qty BETWEEN 1 AND 5", name="ck_order_drafts_qty"),
        Index("uq_order_drafts_open_per_conversation", "conversation_id", unique=True,
              postgresql_where=sql_text("status = 'open'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, default=DRAFT_OPEN, server_default=sql_text("'open'"))
    # +1 on every change; the confirmation must match the version the summary was sent for.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=sql_text("1"))
    design_code: Mapped[str | None] = mapped_column(Text)
    size_code: Mapped[str | None] = mapped_column(Text)
    height_cm: Mapped[float | None] = mapped_column(Numeric(5, 1))
    fit_preference: Mapped[str | None] = mapped_column(Text)
    bust_in: Mapped[float | None] = mapped_column(Numeric(5, 1))
    waist_in: Mapped[float | None] = mapped_column(Numeric(5, 1))
    hip_in: Mapped[float | None] = mapped_column(Numeric(5, 1))
    measurement_notes: Mapped[str | None] = mapped_column(Text)
    option_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, server_default=sql_text("'{}'"))
    qty: Mapped[int] = mapped_column(Integer, default=1, server_default=sql_text("1"))
    delivery_name: Mapped[str | None] = mapped_column(Text)
    delivery_phone: Mapped[str | None] = mapped_column(Text)
    address_line1: Mapped[str | None] = mapped_column(Text)
    address_line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    pincode: Mapped[str | None] = mapped_column(Text)
    last_quote: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary_sent_version: Mapped[int | None] = mapped_column(Integer)
    mybill_order_no: Mapped[str | None] = mapped_column(Text)
    mybill_customer_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProcessedEvent(Base):
    """Events from mybill already handled (dedupe for POST /internal/events)."""

    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    order_no: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawWebhook(Base):
    """Webhook bodies stored as received (Instagram payload capture). Contains customer messages: purge when done."""

    __tablename__ = "raw_webhooks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    channel: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstagramEcho(Base):
    """A message the business account sent on Instagram. Decided after a short delay (under the conversation
    lock): our own send (its mid is a recorded outbound message) or staff typing in the Instagram app."""

    __tablename__ = "instagram_echoes"

    mid: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    text: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(Text)

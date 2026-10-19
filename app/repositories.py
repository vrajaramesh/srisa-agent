"""Database functions. Each takes an AsyncSession; the caller owns the transaction."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DELIVERED,
    FAILED,
    INBOUND,
    OUTBOUND,
    READ,
    RECEIVED,
    SENT,
    SKIPPED,
    Conversation,
    ConversationEntry,
    InstagramEcho,
    LlmTurn,
    Message,
    OrderDraft,
)
from app.webhook_parser import IncomingMessage, StatusUpdate

# A receipt may only move an outbound message forward: sent -> delivered -> read.
# ("failed" can arrive after sent/delivered.) Out-of-order webhooks can't downgrade it.
_STATUS_FROM: dict[str, tuple[str, ...]] = {
    DELIVERED: (SENT,),
    READ: (SENT, DELIVERED),
    FAILED: (SENT, DELIVERED),
}


def _message_time(msg: IncomingMessage) -> datetime:
    """Meta's unix `timestamp` as a datetime (now if missing/invalid)."""
    try:
        return datetime.fromtimestamp(int(msg.timestamp or ""), tz=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


async def upsert_conversation(session: AsyncSession, channel: str, msg: IncomingMessage) -> uuid.UUID:
    """Create the conversation for this sender, or refresh its name / last message time."""
    return await upsert_conversation_for(session, channel, msg.from_, msg.profile_name, _message_time(msg))


async def upsert_conversation_for(session: AsyncSession, channel: str, external_user_id: str, name: str | None,
                                  customer_message_at: datetime | None) -> uuid.UUID:
    """Channel-neutral upsert. customer_message_at=None (e.g. an Instagram echo) leaves the last message time."""
    stmt = pg_insert(Conversation).values(
        id=uuid.uuid4(),
        channel=channel,
        external_user_id=external_user_id,
        customer_name=name,
        last_customer_message_at=customer_message_at,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_conversations_channel_user",
        set_={
            "customer_name": func.coalesce(stmt.excluded.customer_name, Conversation.customer_name),
            "last_customer_message_at": func.greatest(
                stmt.excluded.last_customer_message_at, Conversation.last_customer_message_at
            ),
            "updated_at": func.now(),
        },
    ).returning(Conversation.id)
    return (await session.execute(stmt)).scalar_one()


async def insert_inbound_message(
    session: AsyncSession, conversation_id: uuid.UUID, msg: IncomingMessage, status: str = RECEIVED
) -> uuid.UUID | None:
    """Store an inbound message. Returns None if this Meta message id was already stored."""
    return await insert_inbound(session, conversation_id, msg.id, msg.type, msg.text, msg.reply_id, msg.raw, status)


async def insert_inbound(session: AsyncSession, conversation_id: uuid.UUID, external_message_id: str, message_type: str,
                         text: str | None, reply_id: str | None, raw: dict | None, status: str = RECEIVED) -> uuid.UUID | None:
    """Channel-neutral insert; None if this message id was already stored (duplicate webhook)."""
    stmt = (
        pg_insert(Message)
        .values(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            direction=INBOUND,
            external_message_id=external_message_id,
            type=message_type,
            text=text,
            reply_id=reply_id,
            raw=raw or None,
            status=status,
        )
        .on_conflict_do_nothing(index_elements=["external_message_id"])
        .returning(Message.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_instagram_echo(session: AsyncSession, conversation_id: uuid.UUID, mid: str, text: str | None,
                                raw: dict | None) -> bool:
    """Store an echo for a delayed decision; False if already stored."""
    stmt = (pg_insert(InstagramEcho).values(mid=mid, conversation_id=conversation_id, text=text, raw=raw)
            .on_conflict_do_nothing(index_elements=["mid"]).returning(InstagramEcho.mid))
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def pending_echoes(session: AsyncSession, conversation_id: uuid.UUID) -> list[InstagramEcho]:
    stmt = (select(InstagramEcho)
            .where(InstagramEcho.conversation_id == conversation_id, InstagramEcho.decided_at.is_(None))
            .order_by(InstagramEcho.received_at))
    return list((await session.execute(stmt)).scalars().all())


async def is_our_outbound(session: AsyncSession, external_message_id: str) -> bool:
    """True if we recorded an outbound message with this id (i.e. the echo is of our own send)."""
    stmt = select(Message.id).where(Message.external_message_id == external_message_id, Message.direction == OUTBOUND)
    return (await session.execute(stmt)).first() is not None


async def apply_status(session: AsyncSession, status: StatusUpdate) -> bool:
    """Apply a delivery/read receipt to our outbound message. False if nothing matched."""
    allowed_from = _STATUS_FROM.get(status.status)
    if not allowed_from:
        return False
    result = await session.execute(
        update(Message)
        .where(
            Message.external_message_id == status.id,
            Message.direction == OUTBOUND,
            Message.status.in_(allowed_from),
        )
        .values(status=status.status)
    )
    return result.rowcount > 0


async def lock_conversation(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Transaction-scoped advisory lock: one worker per conversation at a time.

    pg_advisory_xact_lock is released automatically at COMMIT/ROLLBACK, which is safe with
    PgBouncer transaction pooling (session-level locks are not).
    """
    key = func.hashtextextended(str(conversation_id), 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def next_pending_message(
    session: AsyncSession, conversation_id: uuid.UUID, max_attempts: int
) -> Message | None:
    """Oldest inbound message still to answer (received, or failed with attempts left)."""
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == INBOUND,
            or_(Message.status == RECEIVED, (Message.status == FAILED) & (Message.attempts < max_attempts)),
        )
        .order_by(Message.created_at, Message.id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def recent_history(session: AsyncSession, upto: Message, limit: int = 20) -> list[Message]:
    """The conversation's last `limit` messages (both directions) up to and including `upto`."""
    stmt = (
        select(Message)
        .where(Message.conversation_id == upto.conversation_id, Message.created_at <= upto.created_at)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed((await session.execute(stmt)).scalars().all()))


async def pending_inbound(session: AsyncSession, conversation_id: uuid.UUID, max_attempts: int) -> list[Message]:
    """All inbound messages still to answer (received, or failed with attempts left), oldest first.
    The agent answers them together in one run."""
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == INBOUND,
            or_(Message.status == RECEIVED, (Message.status == FAILED) & (Message.attempts < max_attempts)),
        )
        .order_by(Message.created_at, Message.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def agent_history(session: AsyncSession, conversation_id: uuid.UUID, limit: int) -> list[Message]:
    """Last `limit` messages (both directions) for the model, oldest first. Skipped messages (handled by
    the team during a handoff, or from senders outside ALLOWED_SENDERS) are left out."""
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.status != SKIPPED)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed((await session.execute(stmt)).scalars().all()))


def add_llm_turn(session: AsyncSession, conversation_id: uuid.UUID, result: Any) -> LlmTurn:
    """Record one agent run (an app.agent.runner.AgentResult) for cost/latency/debugging."""
    turn = LlmTurn(
        conversation_id=conversation_id,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        tool_calls=result.tool_calls,
        latency_ms=result.latency_ms,
        stop_reason=result.stop_reason,
        error=result.error,
    )
    session.add(turn)
    return turn


def add_outbound_message(
    session: AsyncSession, conversation_id: uuid.UUID, body: str, external_message_id: str | None, raw: dict,
    message_type: str = "text", author: str | None = None,
) -> Message:
    """Record a message we sent (a text reply, or an image with its caption/URL as text)."""
    message = Message(
        conversation_id=conversation_id,
        direction=OUTBOUND,
        external_message_id=external_message_id,
        author=author,
        type=message_type,
        text=body,
        raw=raw or None,
        status=SENT,
    )
    session.add(message)
    return message


async def open_draft(session: AsyncSession, conversation_id: uuid.UUID) -> OrderDraft | None:
    """The conversation's open order draft (at most one, enforced by a partial unique index)."""
    stmt = select(OrderDraft).where(OrderDraft.conversation_id == conversation_id, OrderDraft.status == "open")
    return (await session.execute(stmt)).scalar_one_or_none()


async def last_outbound(session: AsyncSession, conversation_id: uuid.UUID) -> Message | None:
    """Most recent message we sent in this conversation (used to recognise a 'yes' to the summary)."""
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.direction == OUTBOUND)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def latest_order_no(session: AsyncSession, conversation_id: uuid.UUID) -> str | None:
    """mybill order number of the conversation's most recent submitted draft."""
    stmt = (
        select(OrderDraft.mybill_order_no)
        .where(OrderDraft.conversation_id == conversation_id, OrderDraft.mybill_order_no.is_not(None))
        .order_by(OrderDraft.updated_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_entry(session: AsyncSession, conversation_id: uuid.UUID, message_id: uuid.UUID,
                       msg: IncomingMessage) -> None:
    """Store an ad / post referral: a history row, and the conversation's entry_* fields (newest wins).
    entry_resolution and fep_expires_at are reset - a new ad click starts a new entry (the worker fills them)."""
    ref = msg.referral
    if ref is None:
        return
    session.add(ConversationEntry(
        conversation_id=conversation_id, message_id=message_id, source_type=ref.source_type,
        source_id=ref.source_id, source_url=ref.source_url, headline=ref.headline, body=ref.body,
        media_type=ref.media_type, ctwa_clid=ref.ctwa_clid, welcome_text=ref.welcome_text,
        raw=(msg.raw or {}).get("referral"),
    ))
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(entry_source_type=ref.source_type, entry_source_id=ref.source_id, entry_ctwa_clid=ref.ctwa_clid,
                entry_headline=ref.headline, entry_label=None, entry_resolution=None, entry_at=_message_time(msg),
                fep_expires_at=None)
    )


async def previous_inbound_at(session: AsyncSession, conversation_id: uuid.UUID, before: Message) -> datetime | None:
    """When the customer last wrote before `before` (None = `before` starts the conversation)."""
    stmt = (
        select(Message.created_at)
        .where(Message.conversation_id == conversation_id, Message.direction == INBOUND,
               Message.created_at < before.created_at)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()

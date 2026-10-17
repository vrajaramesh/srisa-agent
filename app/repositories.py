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
    LlmTurn,
    Message,
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
    stmt = pg_insert(Conversation).values(
        id=uuid.uuid4(),
        channel=channel,
        external_user_id=msg.from_,
        customer_name=msg.profile_name,
        last_customer_message_at=_message_time(msg),
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
    stmt = (
        pg_insert(Message)
        .values(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            direction=INBOUND,
            external_message_id=msg.id,
            type=msg.type,
            text=msg.text,
            raw=msg.raw or None,
            status=status,
        )
        .on_conflict_do_nothing(index_elements=["external_message_id"])
        .returning(Message.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


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
    message_type: str = "text",
) -> Message:
    """Record a message we sent (a text reply, or an image with its caption/URL as text)."""
    message = Message(
        conversation_id=conversation_id,
        direction=OUTBOUND,
        external_message_id=external_message_id,
        type=message_type,
        text=body,
        raw=raw or None,
        status=SENT,
    )
    session.add(message)
    return message

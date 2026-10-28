"""Daily cleanup of old chat data (Cloud Scheduler -> POST /internal/jobs/retention).

Kept for:
  raw_webhooks (debug payloads, contain customer messages)   7 days
  llm_turns (AI run logs)                                    60 days
  processed_events (dedupe of mybill events / reminders)     90 days
  ref_codes (Instagram -> WhatsApp handoff codes)            30 days after they expire
  instagram_comments                                         1 year
  chats with no message for a year                           deleted with their messages, ad entries, drafts
    ...except chats with an order (a submitted order draft / order created): those are kept.
Daily reports are kept. Orders themselves live in mybill and are never touched here.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, exists, func, or_, select

from app.agent import flow
from app.db import get_sessionmaker
from app.models import (DRAFT_SUBMITTED, Conversation, InstagramComment, LlmTurn, Message, OrderDraft,
                        ProcessedEvent, RawWebhook, RefCode)

logger = logging.getLogger(__name__)

RAW_WEBHOOKS = timedelta(days=7)
LLM_TURNS = timedelta(days=60)
PROCESSED_EVENTS = timedelta(days=90)
REF_CODES = timedelta(days=30)
INSTAGRAM_COMMENTS = timedelta(days=365)
INACTIVE_CHATS = timedelta(days=365)


def _inactive_chats(cutoff: datetime):
    """Chats whose newest message (or creation, if none) is older than cutoff and that never had an order."""
    last_message = select(func.max(Message.created_at)).where(Message.conversation_id == Conversation.id).scalar_subquery()
    has_order = exists().where(OrderDraft.conversation_id == Conversation.id, OrderDraft.status == DRAFT_SUBMITTED)
    return select(Conversation.id).where(
        func.coalesce(last_message, Conversation.created_at) < cutoff,
        Conversation.stage != flow.ORDER_CREATED, ~has_order)


async def run(now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """One cleanup pass in a single transaction. dry_run: only count what would be deleted."""
    now = now or datetime.now(timezone.utc)
    targets = {
        "raw_webhooks": (RawWebhook, RawWebhook.received_at < now - RAW_WEBHOOKS),
        "llm_turns": (LlmTurn, LlmTurn.created_at < now - LLM_TURNS),
        "processed_events": (ProcessedEvent, ProcessedEvent.created_at < now - PROCESSED_EVENTS),
        "ref_codes": (RefCode, or_(RefCode.expires_at < now - REF_CODES,
                                   (RefCode.expires_at.is_(None)) & (RefCode.created_at < now - REF_CODES))),
        "instagram_comments": (InstagramComment, InstagramComment.received_at < now - INSTAGRAM_COMMENTS),
        # messages, ad entries, AI logs, drafts and echoes of these chats go with them (ON DELETE CASCADE)
        "inactive_chats": (Conversation, Conversation.id.in_(_inactive_chats(now - INACTIVE_CHATS))),
    }
    stats: dict[str, Any] = {"dry_run": dry_run}
    async with get_sessionmaker()() as session, session.begin():
        for name, (model, condition) in targets.items():
            if dry_run:
                stats[name] = (await session.execute(select(func.count()).select_from(model).where(condition))).scalar_one()
            else:
                stats[name] = (await session.execute(delete(model).where(condition)
                                                     .execution_options(synchronize_session=False))).rowcount
    logger.info("Retention job done", extra={"fields": stats})
    return stats

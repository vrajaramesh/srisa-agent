"""Message processing: turn a conversation's pending inbound messages into an AI agent reply.

Kept separate from the HTTP route so it can be called by the Cloud Tasks endpoint, the
local BackgroundTask fallback, and tests alike.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import mybill_client, whatsapp_client
from app import repositories as repo
from app.agent import orders, resolution, tools
from app.agent.formatting import split_message
from app.agent.runner import AgentResult, run_agent
from app.config import AgentSettings, get_settings
from app.db import get_sessionmaker
from app.models import DRAFT_OPEN, FAILED, PROCESSED, SKIPPED, Conversation, Message, OrderDraft

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "Thank you! Our team will get back to you shortly 🙏"
MAX_ATTEMPTS = 5  # matches the Cloud Tasks queue's max-attempts

# Taps applied by code before the model runs (then the model continues the conversation).
SHORTCUTS = {"size": ("save_size", "size_code"), "fit": ("save_size", "fit_preference"),
             "opt": ("choose_option", "option_id")}


@dataclass
class ProcessResult:
    """What one worker run did."""

    processed: int = 0
    failed: bool = False


class WorkerMessenger:
    """orders.Messenger over WhatsApp; every successful send is recorded as an outbound message in this
    transaction. The recipient is always the conversation's customer."""

    def __init__(self, session: AsyncSession, conversation: Conversation) -> None:
        self._session, self._conversation = session, conversation
        self.error: str | None = None

    def _record(self, sent: whatsapp_client.SendResult, text: str, message_type: str, meta: dict | None) -> bool:
        if not sent.ok:
            self.error = sent.error or "send failed"
            return False
        repo.add_outbound_message(self._session, self._conversation.id, text, sent.message_id,
                                  {**(sent.data or {}), **(meta or {})}, message_type=message_type)
        return True

    async def send_text(self, text: str) -> bool:
        sent = await whatsapp_client.send_text(self._conversation.external_user_id, text)
        return self._record(sent, text, "text", None)

    async def send_image(self, url: str, caption: str | None) -> bool:
        sent = await whatsapp_client.send_image(self._conversation.external_user_id, url, caption)
        return self._record(sent, f"[photo] {caption or url}", "image", None)

    async def send_buttons(self, body: str, buttons: list[dict[str, str]], meta: dict | None = None) -> bool:
        sent = await whatsapp_client.send_buttons(self._conversation.external_user_id, body, buttons)
        return self._record(sent, body, "interactive", {**(meta or {}), "buttons": buttons})

    async def send_list(self, body: str, button_text: str, sections: list[dict[str, Any]],
                        meta: dict | None = None) -> bool:
        sent = await whatsapp_client.send_list(self._conversation.external_user_id, body, button_text, sections)
        return self._record(sent, body, "interactive", meta)


class WorkerStore:
    """orders.OrderStore over the DB session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def new_draft(self, conversation: Conversation) -> OrderDraft:
        # Flush first so a draft cancelled earlier in this run is saved before the new open one
        # (one open draft per conversation is enforced by a partial unique index).
        await self._session.flush()
        draft = OrderDraft(id=uuid.uuid4(), conversation_id=conversation.id, status=DRAFT_OPEN, version=1,
                           option_ids=[], qty=1)
        self._session.add(draft)
        return draft

    async def get_draft(self, draft_id: uuid.UUID) -> OrderDraft | None:
        return await self._session.get(OrderDraft, draft_id)

    async def latest_order_no(self, conversation: Conversation) -> str | None:
        return await repo.latest_order_no(self._session, conversation.id)


async def generate_reply(conversation: Conversation, history: list[Message], ctx: tools.ToolContext,
                         settings: AgentSettings) -> AgentResult:
    """Decide what to send back: the AI agent (app/agent). Kept as the single seam the worker calls."""
    return await run_agent(conversation, history, ctx, settings)


async def apply_shortcuts(ctx: tools.ToolContext, pending: list[Message]) -> list[dict[str, Any]]:
    """size:<code> / fit:<value> / opt:<id> taps -> the same tool handlers (stage-checked), no model needed."""
    applied = []
    for message in pending:
        prefix, _, value = (message.reply_id or "").partition(":")
        if prefix not in SHORTCUTS or not value:
            continue
        name, arg = SHORTCUTS[prefix]
        result, is_error, _ = await tools.run_tool(ctx, name, {arg: value})
        applied.append({"tap": message.reply_id, "tool": name, "error": result.get("error")})
        logger.info("Tap applied", extra={"fields": {"reply_id": message.reply_id, "error": result.get("error")}})
    return applied


async def process_conversation(conversation_id: uuid.UUID) -> ProcessResult:
    """Answer all pending inbound messages of one conversation in one run.

    Runs in a single transaction holding the conversation's advisory lock (one worker per customer):
    [confirmation path | shortcuts + agent] -> send -> record outbound + llm_turn -> mark processed -> COMMIT.
    A retried task finds nothing pending once a reply is recorded, so it never double-replies.

    - needs_human: messages are stored as skipped and nobody replies (the team has the chat).
    - Confirm tap / "yes" after the summary: code creates the mybill order (no model call).
    - LLM failure / refusal / limits: the fallback text is sent and the chat is handed to the team.
    - WhatsApp send failure / mybill down during confirmation: messages are marked failed (attempts + 1) and
      failed=True is returned, so Cloud Tasks retries; after MAX_ATTEMPTS they are skipped.
    """
    settings = get_settings()
    result = ProcessResult()
    async with get_sessionmaker()() as session, session.begin():
        await repo.lock_conversation(session, conversation_id)
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            logger.warning("Unknown conversation", extra={"fields": {"conversation_id": str(conversation_id)}})
            return result
        pending = await repo.pending_inbound(session, conversation_id, MAX_ATTEMPTS)
        if not pending:
            return result

        if conversation.needs_human:
            # Left unread on purpose, so the team sees them as new in WhatsApp.
            for message in pending:
                message.status, message.last_error = SKIPPED, "needs_human"
            logger.info("Conversation is with the team; not replying",
                        extra={"fields": {"conversation_id": str(conversation_id), "messages": len(pending)}})
            return result

        for message in pending:
            if message.external_message_id:
                await whatsapp_client.mark_as_read(message.external_message_id)

        messenger = WorkerMessenger(session, conversation)
        mybill = mybill_client.get_client()
        ctx = tools.ToolContext(conversation=conversation, mybill=mybill, messenger=messenger,
                                store=WorkerStore(session), draft=await repo.open_draft(session, conversation_id))

        error = await _confirmation_path(session, ctx, pending, mybill)
        if error is None:
            error = await _agent_path(session, ctx, pending, settings)
        if error == "handled":
            error = None

        now = datetime.now(timezone.utc)
        for message in pending:
            if error:
                message.status, message.attempts, message.last_error = FAILED, message.attempts + 1, error
            else:
                message.status, message.processed_at = PROCESSED, now
        if error:
            result.failed = True
            logger.error("Reply failed", extra={"fields": {"conversation_id": str(conversation_id), "error": error}})
        else:
            result.processed = len(pending)
    return result


async def _confirmation_path(session: AsyncSession, ctx: tools.ToolContext, pending: list[Message],
                             mybill: Any) -> str | None:
    """Code-only order confirmation. Returns "handled", an error string (-> retry), or None (not a confirmation)."""
    confirmed = orders.find_confirmation(pending, await repo.last_outbound(session, ctx.conversation.id), ctx.draft)
    if confirmed is None or mybill is None:
        return None
    outcome = await orders.confirm(ctx.conversation, ctx.draft, confirmed, ctx.store, mybill, ctx.messenger, ctx.design)
    logger.info("Confirmation", extra={"fields": {"conversation_id": str(ctx.conversation.id), "outcome": outcome.kind,
                                                  "order_no": outcome.order_no}})
    if outcome.kind in ("created", "already_submitted", "resent_summary"):
        return "handled"
    if outcome.kind in ("unknown_draft", "not_ready"):
        return None  # let the model continue (it sees "[tapped: ✅ Confirm]")
    if outcome.kind == "unavailable" and max(m.attempts for m in pending) + 1 < MAX_ATTEMPTS:
        return "mybill unavailable"  # retry; the idempotency key makes it safe
    if outcome.kind == "send_failed":
        return ctx.messenger.error or "send failed"
    # rejected, or still unavailable on the last attempt: tell her and hand over.
    ctx.conversation.needs_human = True
    ctx.conversation.needs_human_reason = f"order_failed: {outcome.kind} {outcome.detail or ''}".strip()
    logger.error("Order could not be created", extra={"fields": {
        "conversation_id": str(ctx.conversation.id), "outcome": outcome.kind, "detail": outcome.detail}})
    if not await ctx.messenger.send_text(FALLBACK_REPLY):
        ctx.conversation.needs_human, ctx.conversation.needs_human_reason = False, None
        return ctx.messenger.error or "send failed"
    return "handled"


async def _agent_path(session: AsyncSession, ctx: tools.ToolContext, pending: list[Message],
                      settings: AgentSettings) -> str | None:
    """Shortcuts, then the AI agent; sends its reply. Returns None on success or an error string (-> retry)."""
    conversation = ctx.conversation
    await apply_shortcuts(ctx, pending)
    await resolution.resolve_design_code(conversation, [m.text for m in pending if not m.reply_id], ctx.mybill)
    history = await repo.agent_history(session, conversation.id, settings.history_messages)
    agent = await generate_reply(conversation, history, ctx, settings)
    repo.add_llm_turn(session, conversation.id, agent)

    if agent.fallback:
        logger.error("Agent failed; sending fallback and handing over",
                     extra={"fields": {"conversation_id": str(conversation.id), "error": agent.error}})
    replies = [FALLBACK_REPLY] if agent.fallback else agent.replies

    for reply in replies:
        for chunk in split_message(reply):
            if not await ctx.messenger.send_text(chunk):
                # The customer got nothing (or only part): don't hand over yet, or the retry would stay silent.
                conversation.needs_human, conversation.needs_human_reason = False, None
                return ctx.messenger.error or "send failed"
    if agent.fallback:
        conversation.needs_human = True
        conversation.needs_human_reason = f"llm_failure: {agent.error}"
    return None

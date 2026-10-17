"""Message processing: turn a conversation's pending inbound messages into an AI agent reply.

Kept separate from the HTTP route so it can be called by the Cloud Tasks endpoint, the
local BackgroundTask fallback, and tests alike.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app import mybill_client, whatsapp_client
from app import repositories as repo
from app.agent import resolution, tools
from app.agent.formatting import split_message
from app.agent.runner import AgentResult, run_agent
from app.config import AgentSettings, get_settings
from app.db import get_sessionmaker
from app.models import FAILED, PROCESSED, SKIPPED, Conversation, Message

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "Thank you! Our team will get back to you shortly 🙏"
MAX_ATTEMPTS = 5  # matches the Cloud Tasks queue's max-attempts


@dataclass
class ProcessResult:
    """What one worker run did."""

    processed: int = 0
    failed: bool = False


async def generate_reply(conversation: Conversation, history: list[Message], ctx: tools.ToolContext,
                         settings: AgentSettings) -> AgentResult:
    """Decide what to send back: the AI agent (app/agent). Kept as the single seam the worker calls."""
    return await run_agent(conversation, history, ctx, settings)


def _image_sender(session: AsyncSession, conversation_id: uuid.UUID) -> tools.ImageSender:
    """send_image for the tools: sends via WhatsApp and records the outbound image in this transaction."""

    async def send(to: str, url: str, caption: str | None) -> str | None:
        sent = await whatsapp_client.send_image(to, url, caption)
        if not sent.ok:
            return None
        repo.add_outbound_message(session, conversation_id, f"[photo] {caption or url}", sent.message_id,
                                  sent.data, message_type="image")
        return sent.message_id or "sent"

    return send


async def process_conversation(conversation_id: uuid.UUID) -> ProcessResult:
    """Answer all pending inbound messages of one conversation in one agent run.

    Runs in a single transaction holding the conversation's advisory lock (one worker per
    customer): agent -> send -> record outbound + llm_turn -> mark inbound processed -> COMMIT.
    A retried task finds nothing pending once a reply is recorded, so it never double-replies.

    - needs_human: messages are stored as skipped and nobody replies (the team has the chat).
    - LLM failure / refusal / limits: the fallback text is sent and the chat is handed to the team.
    - WhatsApp send failure: the messages are marked failed (attempts + 1) and failed=True is
      returned, so the caller answers 500 and Cloud Tasks retries; after MAX_ATTEMPTS they are skipped.
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

        mybill = mybill_client.get_client()
        await resolution.resolve_design_code(conversation, [m.text for m in pending], mybill)
        history = await repo.agent_history(session, conversation_id, settings.history_messages)
        ctx = tools.ToolContext(conversation=conversation, mybill=mybill,
                                send_image=_image_sender(session, conversation_id))
        agent = await generate_reply(conversation, history, ctx, settings)
        repo.add_llm_turn(session, conversation_id, agent)

        if agent.fallback:
            logger.error("Agent failed; sending fallback and handing over",
                         extra={"fields": {"conversation_id": str(conversation_id), "error": agent.error}})
        replies = [FALLBACK_REPLY] if agent.fallback else agent.replies

        error = None
        for reply in replies:
            for chunk in split_message(reply):
                sent = await whatsapp_client.send_text(conversation.external_user_id, chunk)
                if not sent.ok:
                    error = sent.error or "send failed"
                    break
                repo.add_outbound_message(session, conversation_id, chunk, sent.message_id, sent.data)
            if error:
                break

        now = datetime.now(timezone.utc)
        for message in pending:
            if error:
                message.status, message.attempts, message.last_error = FAILED, message.attempts + 1, error
            else:
                message.status, message.processed_at = PROCESSED, now
        if error:
            # The customer got nothing (or only part): don't hand over yet, or the retry would stay silent.
            conversation.needs_human, conversation.needs_human_reason = False, None
            result.failed = True
            logger.error("Reply failed", extra={"fields": {"conversation_id": str(conversation_id), "error": error}})
        else:
            if agent.fallback:
                conversation.needs_human = True
                conversation.needs_human_reason = f"llm_failure: {agent.error}"
            result.processed = len(pending)
    return result

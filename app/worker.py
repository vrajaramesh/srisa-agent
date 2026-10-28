"""Message processing: turn a conversation's pending inbound messages into an AI agent reply.

Kept separate from the HTTP route so it can be called by the Cloud Tasks endpoint, the
local BackgroundTask fallback, and tests alike.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import channels, firms, mybill_client, staff_alerts
from app import repositories as repo
from app.agent import ad_entry, fep, ig_handoff, orders, resolution, tools
from app.agent.runner import AgentResult, run_agent
from app.channels import instagram
from app.channels.base import ChannelAdapter, SendResult
from app.webhook_parser import Referral
from app.config import AgentSettings, get_settings
from app.db import get_sessionmaker
from app.models import DRAFT_OPEN, FAILED, PROCESSED, SKIPPED, Conversation, Message, OrderDraft

INSTAGRAM_REPLY_WINDOW_HOURS = 24

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "Thank you! Our team will get back to you shortly 🙏"
MAX_ATTEMPTS = 5  # matches the Cloud Tasks queue's max-attempts
EVENT_TIMEOUT_SECONDS = 8  # background work (marketing events, staff alerts) is awaited at most this long

# Taps applied by code before the model runs (then the model continues the conversation).
SHORTCUTS = {"size": ("save_size", "size_code"), "fit": ("save_size", "fit_preference"),
             "opt": ("choose_option", "option_id")}


@dataclass
class ProcessResult:
    """What one worker run did."""

    processed: int = 0
    failed: bool = False


class WorkerMessenger:
    """orders.Messenger over the conversation's channel (ChannelAdapter); every successful send is recorded as an
    outbound message in this transaction. The recipient is always the conversation's customer."""

    def __init__(self, session: AsyncSession, conversation: Conversation, adapter: ChannelAdapter | None = None,
                 author: str = "bot") -> None:
        self._session, self._conversation = session, conversation
        self.adapter = adapter or channels.adapter_for(conversation.channel, firms.get(conversation.firm))
        self.author = author
        self.error: str | None = None
        self.sent = 0  # messages successfully sent in this run

    @property
    def _to(self) -> str:
        return self._conversation.external_user_id

    def _record(self, sent: SendResult, text: str, message_type: str, meta: dict | None) -> bool:
        if not sent.ok:
            self.error = sent.error or "send failed"
            return False
        repo.add_outbound_message(self._session, self._conversation.id, text, sent.message_id,
                                  {**(sent.data or {}), **(meta or {})}, message_type=message_type, author=self.author)
        self.sent += 1
        return True

    async def send_text(self, text: str) -> bool:
        """Split for the channel (WhatsApp: 1000 characters, Instagram: 1000 bytes); every part is recorded."""
        for part in self.adapter.split_text(text):
            if not self._record(await self.adapter.send_text(self._to, part), part, "text", None):
                return False
        return True

    async def send_text_meta(self, text: str, meta: dict) -> bool:
        """Like send_text, with extra fields on every recorded part (e.g. {"followup": "payment"})."""
        for part in self.adapter.split_text(text):
            if not self._record(await self.adapter.send_text(self._to, part), part, "text", meta):
                return False
        return True

    async def send_image(self, url: str, caption: str | None) -> bool:
        if self.adapter.image_captions:
            return self._record(await self.adapter.send_image(self._to, url, caption), f"[photo] {caption or url}",
                                "image", {"image_url": url})
        # Instagram: no captions on images - the caption follows as its own (recorded) text message.
        if not self._record(await self.adapter.send_image(self._to, url, None), f"[photo] {url}", "image",
                            {"image_url": url}):
            return False
        return await self.send_text(caption) if caption else True

    async def _send_choices(self, body: str, choices: list[dict[str, str]], style: str, button_text: str,
                            section_title: str | None, meta: dict | None) -> bool:
        sent = await self.adapter.send_choices(self._to, body, choices, style, button_text, section_title)
        if sent.ok and sent.data.get("offered"):
            self._conversation.last_offered_choices = sent.data["offered"]
        return self._record(sent, body, "interactive", meta)

    async def send_buttons(self, body: str, buttons: list[dict[str, str]], meta: dict | None = None) -> bool:
        return await self._send_choices(body, buttons, "buttons", "Choose", None, {**(meta or {}), "buttons": buttons})

    async def send_list(self, body: str, button_text: str, sections: list[dict[str, Any]],
                        meta: dict | None = None) -> bool:
        rows = [row for section in sections for row in section.get("rows", [])]
        title = sections[0].get("title") if sections else None
        return await self._send_choices(body, rows, "list", button_text, title, meta)

    async def send_link_button(self, body: str, button_text: str, url: str, image_url: str | None = None) -> bool:
        return self._record(await self.adapter.send_link_button(self._to, body, button_text, url, image_url),
                            f"{body}\n{url}", "interactive", {"link": url, **({"image_url": image_url} if image_url else {})})


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

    async def create_ref_code(self, conversation: Conversation, design_code: str | None) -> str:
        return await repo.create_ref_code(self._session, conversation.external_user_id, design_code)


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
    - LLM failure / refusal / limits: the chat is handed to the team (no message to her; staff are alerted).
    - WhatsApp send failure / mybill down during confirmation: messages are marked failed (attempts + 1) and
      failed=True is returned, so Cloud Tasks retries; after MAX_ATTEMPTS they are skipped.
    """
    background: list[asyncio.Task] = []
    try:
        return await _process(conversation_id, background)
    finally:
        # Fire-and-forget work (marketing events) started during the run: give it a few seconds after the
        # reply went out, so Cloud Run doesn't freeze it once the request ends. Never fails the run.
        if background:
            await asyncio.wait(background, timeout=EVENT_TIMEOUT_SECONDS)
            for task in background:
                if not task.done():
                    task.cancel()


async def _process(conversation_id: uuid.UUID, background: list[asyncio.Task]) -> ProcessResult:
    settings = get_settings()
    result = ProcessResult()
    async with get_sessionmaker()() as session, session.begin():
        await repo.lock_conversation(session, conversation_id)
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            logger.warning("Unknown conversation", extra={"fields": {"conversation_id": str(conversation_id)}})
            return result
        was_needs_human = bool(conversation.needs_human)
        if conversation.channel == "instagram":
            await _decide_echoes(session, conversation)
        pending = await repo.pending_inbound(session, conversation_id, MAX_ATTEMPTS)
        if conversation.dismissed_at:      # removed by staff: what she wrote before that is not answered
            for message in [m for m in pending if m.created_at <= conversation.dismissed_at]:
                message.status, message.last_error = SKIPPED, "dismissed"
            pending = [m for m in pending if m.created_at > conversation.dismissed_at]
        if not pending:
            return result

        if conversation.needs_human:
            # Left unread on purpose, so the team sees them as new in WhatsApp.
            for message in pending:
                message.status, message.last_error = SKIPPED, "needs_human"
            logger.info("Conversation is with the team; not replying",
                        extra={"fields": {"conversation_id": str(conversation_id), "messages": len(pending)}})
            return result

        messenger = WorkerMessenger(session, conversation)
        if conversation.channel == "instagram":
            if _outside_instagram_window(conversation):
                for message in pending:
                    message.status, message.last_error = SKIPPED, "outside_24h"
                logger.info("Instagram reply window closed; not replying",
                            extra={"fields": {"conversation_id": str(conversation_id)}})
                return result
            await messenger.adapter.mark_seen(conversation.external_user_id, None)
            await _fetch_instagram_profile(conversation)
        else:
            for message in pending:
                if message.external_message_id:
                    await messenger.adapter.mark_seen(conversation.external_user_id, message.external_message_id)
        map_numbered_replies(conversation, pending)

        base = mybill_client.get_client()
        mybill = mybill_client.FirmClient(base, conversation) if base is not None else None   # the chat's firm
        ctx = tools.ToolContext(conversation=conversation, mybill=mybill, messenger=messenger,
                                store=WorkerStore(session), draft=await repo.open_draft(session, conversation_id))

        entry_msg, referral = ad_entry.entry_message(pending)
        if referral is None and conversation.channel == "whatsapp":
            entry_msg, referral = await _inferred_ad_entry(session, conversation, pending)
        handoff_msg, ref = (None, None)
        if referral is None and conversation.channel == "whatsapp":
            handoff_msg, ref = ig_handoff.find_ref(pending)
        # A wholesale code (SLW-1) in the ad / post or her message: the chat belongs to the wholesale firm.
        firms.detect(conversation, ([referral.body, referral.headline] if referral is not None else [])
                     + [m.text for m in pending if not m.reply_id])
        error = await _confirmation_path(session, ctx, pending, mybill)
        if error is None and referral is not None:
            error = await _ad_entry_path(session, ctx, pending, entry_msg, referral, background)
        elif error is None and ref is not None:
            error = await _instagram_handoff_path(session, ctx, pending, handoff_msg, ref)
        if error is None:
            error = await _agent_path(session, ctx, pending, settings, resolved=referral is not None or ref is not None)
        if error == "handled":
            error = None

        now = datetime.now(timezone.utc)
        if not error and referral is not None and not referral.inferred and messenger.sent and fep.opens_fep(
                conversation, ad_entry.message_time(entry_msg), now):
            conversation.fep_expires_at = fep.fep_expiry(now)
            logger.info("Free entry point window opened", extra={"fields": {
                "conversation_id": str(conversation_id), "expires_at": conversation.fep_expires_at.isoformat()}})
        for message in pending:
            if error:
                message.status, message.attempts, message.last_error = FAILED, message.attempts + 1, error
            else:
                message.status, message.processed_at = PROCESSED, now
        if error:
            result.failed = True
            logger.error("Reply failed", extra={"fields": {"conversation_id": str(conversation_id), "error": error}})
        if staff_alerts.should_alert(was_needs_human, conversation):
            background.append(asyncio.create_task(staff_alerts.alert_staff(conversation)))
        else:
            result.processed = len(pending)
    return result


async def _confirmation_path(session: AsyncSession, ctx: tools.ToolContext, pending: list[Message],
                             mybill: Any) -> str | None:
    """Code-only order confirmation. Returns "handled", an error string (-> retry), or None (not a confirmation)."""
    confirmed = orders.find_confirmation(pending, await repo.last_outbound(session, ctx.conversation.id), ctx.draft)
    if confirmed is None or mybill is None:
        return None
    outcome = await orders.confirm(ctx.conversation, ctx.draft, confirmed, ctx.store, mybill, ctx.messenger, ctx.design,
                                  ctx.fabric)
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


async def _report_event(mybill: Any, event: dict[str, Any]) -> None:
    try:
        await mybill.report_marketing_event(event)
        logger.info("Marketing event reported", extra={"fields": {"source_id": event.get("source_id")}})
    except Exception as exc:  # reporting must never affect the conversation
        logger.warning("Marketing event not reported", extra={"fields": {"error": repr(exc)}})


async def _inferred_ad_entry(session: AsyncSession, conversation: Conversation,
                             pending: list[Message]) -> tuple[Message, Any] | tuple[None, None]:
    """A new chat that opens with Meta's default ad message but no referral (Meta drops it for some taps):
    treat it as coming from the ad that brought the latest chats (last 3 days). Not reported to mybill."""
    first = pending[0]
    if not ad_entry.is_ad_greeting(first.text) or await repo.previous_inbound_at(session, conversation.id, first):
        return None, None
    entry = await repo.latest_ad_entry(session, datetime.now(timezone.utc) - ad_entry.INFERRED_AD_WINDOW)
    if entry is None:
        return None, None
    logger.info("Ad entry inferred from greeting", extra={"fields": {
        "conversation_id": str(conversation.id), "source_id": entry.source_id}})
    return first, ad_entry.inferred_referral(entry)


async def _ad_entry_path(session: AsyncSession, ctx: tools.ToolContext, pending: list[Message], entry_msg: Message,
                         referral: Any, background: list[asyncio.Task]) -> str | None:
    """Chat opened from an ad / post: resolve the design (ad link, ad text, her text), report the conversation,
    and for a fresh start send the deterministic first reply. Returns "handled", an error (-> retry), or None
    (the model continues; the facts block tells it about the ad)."""
    conversation = ctx.conversation
    resolved = await resolution.resolve_entry(conversation, referral,
                                              [m.text for m in pending if not m.reply_id], ctx.mybill)
    if entry_msg.attempts == 0 and ctx.mybill is not None and not referral.inferred:
        event = ad_entry.marketing_event(conversation, referral, ad_entry.message_time(entry_msg))
        if event:
            background.append(asyncio.create_task(_report_event(ctx.mybill, event)))
    if resolved.design is None and resolved.collection is None:
        return None
    if firms.get(conversation.firm).wholesale:
        return None   # wholesale: the AI writes the first reply from the collection's terms (MOQ, prices)
    previous = await repo.previous_inbound_at(session, conversation.id, entry_msg)
    if not ad_entry.is_fresh_start(previous, entry_msg):
        return None
    if resolved.collection is not None:
        sent = await ad_entry.send_collection_reply(ctx, resolved.collection)
    else:
        sent = await ad_entry.send_first_reply(ctx, referral, resolved.design)
    if not sent:
        return ctx.messenger.error or "send failed"
    logger.info("Ad first reply sent", extra={"fields": {"conversation_id": str(conversation.id),
                                                         "code": (resolved.collection or resolved.design).get("code")}})
    if any(ad_entry.asks_something(m) for m in pending):
        return None   # she also asked something ("Price", "readymade?"): the agent answers it next
    return "handled"


async def _instagram_handoff_path(session: AsyncSession, ctx: tools.ToolContext, pending: list[Message],
                                  handoff_msg: Message, ref: str) -> str | None:
    """She pressed Send on the pre-typed message from Instagram ("... order KUR-101 (ref IG-XXXXX)"): link her
    Instagram account to this number (mybill merges the two customers), resolve the design and, for a fresh chat,
    start like the ad flow (photo + Order / Photos / Size guide). Returns "handled", an error, or None (model)."""
    conversation = ctx.conversation
    now = datetime.now(timezone.utc)
    code = await repo.get_ref_code(session, ref)
    valid = code is not None and (code.expires_at is None or code.expires_at > now)
    candidates: list[str] = []
    if valid:
        if code.wa_id is None:
            code.wa_id, code.used_at = conversation.external_user_id, now
        conversation.entry_source_type, conversation.entry_source_id = "instagram", code.ig_user_id
        conversation.entry_ctwa_clid = conversation.entry_headline = conversation.fep_expires_at = None
        conversation.entry_label, conversation.entry_at = f"Instagram DM ({ref})", now
        if code.design_code:
            candidates.append(code.design_code)
        if ctx.mybill is not None:
            try:   # phone + Instagram id together: mybill merges the two customers if they differ
                customer = await ctx.mybill.upsert_customer(phone_e164=orders.phone_e164(conversation),
                                                            instagram_user_id=code.ig_user_id,
                                                            name=conversation.customer_name)
                conversation.mybill_customer_id = customer.get("customer_id")
            except Exception as exc:  # noqa: BLE001 - linking is best effort; the order flow works without it
                logger.warning("Instagram link not saved in mybill", extra={"fields": {"ref": ref, "error": repr(exc)[:200]}})
    else:
        logger.info("Unknown or expired Instagram ref", extra={"fields": {"ref": ref}})
    candidates += resolution.find_design_codes(handoff_msg.text)
    design = collection = None
    for candidate in candidates:
        if ctx.mybill is not None and (design := await resolution._active_design(ctx.mybill, candidate)):
            break
    if design is None and ctx.mybill is not None:
        # a collection / item code (e.g. MCS-1 or MCS-1B)
        for candidate in candidates + resolution.find_collection_codes(handoff_msg.text):
            if collection := await resolution._active_collection(ctx.mybill, candidate):
                break
    if design is not None:
        conversation.current_product_code = design.get("code")
    if collection is not None:
        conversation.current_collection_code = collection.get("code")
    if valid:
        conversation.entry_resolution = "ig_handoff" if (design or collection) is not None else "none"
    logger.info("Instagram handoff", extra={"fields": {"conversation_id": str(conversation.id), "ref": ref,
                                                        "linked": valid,
                                                        "code": (design or collection or {}).get("code")}})
    if design is None and collection is None:
        return None
    if firms.get(conversation.firm).wholesale:
        return None   # wholesale: the AI writes the first reply from the collection's terms
    previous = await repo.previous_inbound_at(session, conversation.id, handoff_msg)
    if not ad_entry.is_fresh_start(previous, handoff_msg):
        return None
    sent = (await ad_entry.send_collection_reply(ctx, collection) if collection is not None
            else await ad_entry.send_first_reply(ctx, Referral(welcome_text=None), design))
    if not sent:
        return ctx.messenger.error or "send failed"
    return "handled"


async def _agent_path(session: AsyncSession, ctx: tools.ToolContext, pending: list[Message],
                      settings: AgentSettings, resolved: bool = False) -> str | None:
    """Shortcuts, then the AI agent; sends its reply. Returns None on success or an error string (-> retry).
    resolved=True: the ad entry path already resolved the design for these messages."""
    conversation = ctx.conversation
    await ad_entry.apply_taps(ctx, pending)
    await apply_shortcuts(ctx, pending)
    if not resolved:
        await resolution.resolve_design_code(conversation, [m.text for m in pending if not m.reply_id], ctx.mybill)
    history = await repo.agent_history(session, conversation.id, settings.history_messages)
    agent = await generate_reply(conversation, history, ctx, settings)
    repo.add_llm_turn(session, conversation.id, agent)

    if agent.fallback:
        logger.error("Agent failed; handing over (no message to the customer)",
                     extra={"fields": {"conversation_id": str(conversation.id), "error": agent.error}})
    replies = [] if agent.fallback else agent.replies   # hand-overs are silent; the team is alerted

    for reply in replies:
        if not await ctx.messenger.send_text(reply):
            # The customer got nothing (or only part): don't hand over yet, or the retry would stay silent.
            conversation.needs_human, conversation.needs_human_reason = False, None
            return ctx.messenger.error or "send failed"
    if agent.fallback:
        conversation.needs_human = True
        conversation.needs_human_reason = f"llm_failure: {agent.error}"
    return None


# ── Instagram ────────────────────────────────────────────────────────────────

def _outside_instagram_window(conversation: Conversation, now: datetime | None = None) -> bool:
    """Instagram allows replies only within 24 h of the customer's last message (no templates)."""
    last = conversation.last_customer_message_at
    return last is None or (now or datetime.now(timezone.utc)) - last > timedelta(hours=INSTAGRAM_REPLY_WINDOW_HOURS)


async def _fetch_instagram_profile(conversation: Conversation) -> None:
    """Name/username once per conversation (failures are ignored and not retried)."""
    if conversation.profile_fetched_at is not None:
        return
    conversation.profile_fetched_at = datetime.now(timezone.utc)
    profile = await instagram.fetch_profile(conversation.external_user_id, firms.get(conversation.firm))
    if profile:
        conversation.profile_username = profile.get("username")
        if not conversation.customer_name:
            conversation.customer_name = profile.get("name") or profile.get("username")


async def _decide_echoes(session: AsyncSession, conversation: Conversation) -> None:
    """Echoes of our own sends are ignored; anything else was typed by staff in the Instagram app -> they take over.
    Runs under the conversation lock, so every send of an earlier run is already recorded."""
    for echo in await repo.pending_echoes(session, conversation.id):
        echo.decided_at = datetime.now(timezone.utc)
        if await repo.is_our_outbound(session, echo.mid):
            echo.decision = "ours"
            continue
        echo.decision = "staff"
        repo.add_outbound_message(session, conversation.id, echo.text or "[staff message]", echo.mid, echo.raw or {},
                                  message_type="text", author="staff")
        if not conversation.needs_human:
            conversation.needs_human, conversation.needs_human_reason = True, "staff_took_over"
        logger.info("STAFF_TOOK_OVER", extra={"fields": {"conversation_id": str(conversation.id), "mid": echo.mid}})


def map_numbered_replies(conversation: Conversation, pending: list[Message]) -> None:
    """A typed "2" after we offered choices becomes a tap on choice 2 (reply_id = its id). Any other message
    clears the offer, so a later number (e.g. a quantity) is not mistaken for a choice."""
    for message in pending:
        offered = conversation.last_offered_choices or []
        text = (message.text or "").strip()
        if message.reply_id is None and message.type == "text" and offered and text.isdigit():
            choice = next((c for c in offered if str(c.get("n")) == text), None)
            if choice:
                message.reply_id, message.type, message.text = choice["id"], "interactive", choice["title"]
        conversation.last_offered_choices = None

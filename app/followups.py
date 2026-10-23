"""Gentle follow-ups for customers who went quiet (run every 30 minutes by Cloud Scheduler).

Only inside WhatsApp's free 24 h window (normal messages, no templates), only between 9 AM and 9 PM IST, never
after she said no/stop, never while the team has the chat, never after she paid:

- enquiry       - she asked about a design, we answered, she went silent: one reminder ~20 h after her last message
                  (or the last daytime moment before the window closes, at least 12 h after her message).
- enquiry_last  - reminders 2, 3, ...: each time she answered the previous reminder and went quiet again (same
                  timing). If she doesn't answer a reminder, nothing more is sent; "no"/"stop" ends it for good.
- payment       - her order is created but not paid: reminders ~2 h and ~20 h after the order, with a fresh link.

Each reminder is recorded as a bot message with raw.followup = kind (the inbox shows a "Reminder" tag).
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import mybill_client
from app import repositories as repo
from app.agent import flow, orders, tools
from app.agent.formatting import format_rupees
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import DRAFT_SUBMITTED, INBOUND, Conversation, Message, OrderDraft, ProcessedEvent
from app.worker import WorkerMessenger, WorkerStore

logger = logging.getLogger(__name__)

ENQUIRY, ENQUIRY_LAST, PAYMENT = "enquiry", "enquiry_last", "payment"
WINDOW = timedelta(hours=24)
MARGIN = timedelta(minutes=20)          # don't start a send right at the window's edge
# A clear opt-out anywhere in her last week of messages stops all reminders.
OPTED_OUT = re.compile(r"(?i)\b(stop|unsubscribe|not interested|no thanks|no thank you|don'?t (message|msg|send|text)"
                       r"|do not (message|msg|send|text)|no need)\b")
def is_decline(text: str | None) -> bool:
    """A short refusal ("No", "No thanks", "Later", "Nahi") - not "No, I want XL"."""
    text = (text or "").strip()
    return bool(DECLINED.search(text)) and len(text.split()) <= 4 and "," not in text


IST = timezone(timedelta(hours=5, minutes=30))
DAY_START, DAY_END = 9, 21              # reminders only 09:00-21:00 IST
JOB_INTERVAL = timedelta(minutes=30)
RUN_BUDGET_SECONDS = 240                # Cloud Run allows 300 s per request
EARLIEST = timedelta(hours=12)          # never remind sooner than this after her last message
DECLINED = re.compile(r"(?i)^\W*(no|nope|not interested|no thanks|no thank you|stop|don'?t|dont|later|"
                      r"nahi|nahin|vaddu|vaddhu|venda|na)\b")


def _first_name(conversation: Conversation) -> str:
    return (conversation.customer_name or "").strip().split(" ")[0]


def _hi(conversation: Conversation) -> str:
    first = _first_name(conversation)
    return f"Hi {first}! 😊" if first else "Hi! 😊"


def in_free_window(conversation: Conversation, now: datetime) -> bool:
    """A normal (non-template) message may be sent now: within 24 h of her last message."""
    last = conversation.last_customer_message_at
    return bool(last and now < last + WINDOW - MARGIN)


def is_daytime(moment: datetime) -> bool:
    return DAY_START <= moment.astimezone(IST).hour < DAY_END


def _daytime_between(start: datetime, end: datetime) -> bool:
    t = start
    while t <= end:
        if is_daytime(t):
            return True
        t += timedelta(minutes=15)
    return False


def reminder_due(since: datetime, now: datetime, target: timedelta) -> bool:
    """Send now? In daytime, at least EARLIEST after `since`, inside the window, and either the target time has come
    or this is the last daytime run before the window closes (so a night-time target moves to the evening before)."""
    deadline = since + WINDOW - MARGIN
    if not is_daytime(now) or now - since < EARLIEST or now >= deadline:
        return False
    return now - since >= target or not _daytime_between(now + JOB_INTERVAL, deadline)


async def _messages_since(session, conversation_id, since: datetime) -> list[Message]:
    return list((await session.execute(
        select(Message).where(Message.conversation_id == conversation_id, Message.created_at >= since)
        .order_by(Message.created_at))).scalars().all())


async def _recent_inbound(session, conversation_id, now: datetime) -> list[Message]:
    return list((await session.execute(
        select(Message).where(Message.conversation_id == conversation_id, Message.direction == INBOUND,
                              Message.created_at >= now - timedelta(days=7)))).scalars().all())


async def _followups_sent(session, conversation_id, kinds: tuple[str, ...], since: datetime) -> int:
    return (await session.execute(
        select(func.count()).select_from(Message).where(
            Message.conversation_id == conversation_id, Message.direction != INBOUND,
            Message.created_at >= since, Message.raw["followup"].astext.in_(kinds)))).scalar_one()


def enquiry_text(conversation: Conversation, design: dict[str, Any] | None, last: bool) -> str:
    hi = _hi(conversation)
    if design is None:
        return (f"{hi} Just checking – did you find what you were looking for? Reply here anytime, happy to help "
                f"with designs, sizes or fabric.")
    name = f"{design['name']} ({design['code']})"
    lead = design.get("lead_time_days")
    stitched = f"we stitch it to your size in {lead} days" if lead else "we stitch it to your size"
    if last:
        return f"{hi} {name} is still available 💜 Tap *Order this* and we'll take your size, or ask me anything."
    return f"{hi} Just checking – any questions about {name}? It's {format_rupees(design.get('base_price'))} and {stitched}."


def enquiry_buttons(design: dict[str, Any]) -> list[dict[str, str]]:
    code = design["code"]
    buttons = [{"id": f"ad:order:{code}", "title": "🛍️ Order this"}, {"id": f"ad:photos:{code}", "title": "📸 More photos"}]
    if tools.design_fabrics(design):
        buttons.append({"id": f"fu:fabric:{code}", "title": "🧵 Fabric only"})
    else:
        buttons.append({"id": "ad:sizes", "title": "📏 Size guide"})
    return buttons


async def _enquiry(session, conversation: Conversation, now: datetime, mybill: Any, settings: Any) -> str | None:
    """Kind of enquiry reminder due now (or None)."""
    last_in = conversation.last_customer_message_at
    if last_in is None or flow.stage_of(conversation) == flow.ORDER_CREATED:
        return None
    msgs = await _messages_since(session, conversation.id, last_in - timedelta(seconds=1))
    inbound = [m for m in msgs if m.direction == INBOUND]
    if not any(m.direction != INBOUND for m in msgs):
        return None                              # we haven't answered her yet - not a "silent" customer
    if inbound and is_decline(inbound[-1].text):
        return None                              # a short "no", "later", "nahi" as her last word
    if await _followups_sent(session, conversation.id, (ENQUIRY, ENQUIRY_LAST), last_in) > 0:
        return None                              # already reminded for this silence; she hasn't answered since
    if not reminder_due(last_in, now, timedelta(hours=settings.followup_enquiry_hours)):
        return None
    if any(OPTED_OUT.search(m.text or "") for m in await _recent_inbound(session, conversation.id, now)):
        return None                              # "stop" / "not interested" at some point in the last week
    # A later reminder is only reached after she answered the previous one (her newer message started this silence).
    earlier = await _followups_sent(session, conversation.id, (ENQUIRY, ENQUIRY_LAST), now - timedelta(days=7))
    return ENQUIRY if earlier == 0 else ENQUIRY_LAST


async def _send_enquiry(conversation: Conversation, messenger: WorkerMessenger, ctx: tools.ToolContext, kind: str) -> bool:
    design = None
    if conversation.current_product_code:
        try:
            design = await ctx.design(conversation.current_product_code)
        except Exception:  # noqa: BLE001 - fall back to the generic text
            design = None
    meta = {"followup": kind}
    if design is None and conversation.current_collection_code and ctx.mybill is not None:
        try:
            collection = await ctx.mybill.get_collection(conversation.current_collection_code)
        except Exception:  # noqa: BLE001 - fall back to the generic text
            collection = None
        if collection:
            how = "the code and metres" if collection.get("type") == "FABRIC" else "the code and your size"
            text = (f"{_hi(conversation)} Just checking – did you like any of the {collection['name']} "
                    f"({collection['code']})? Reply with {how} and I'll prepare your order.")
            return await messenger.send_text_meta(text, meta)
    text = enquiry_text(conversation, design, kind == ENQUIRY_LAST)
    if design is None:
        return await messenger.send_text_meta(text, meta)
    return await messenger.send_buttons(text, enquiry_buttons(design), {**meta, "kind": "followup", "code": design["code"]})


async def _payment(session, conversation: Conversation, now: datetime, settings: Any) -> OrderDraft | None:
    """The submitted, unpaid order a payment reminder is due for (or None)."""
    if flow.stage_of(conversation) != flow.ORDER_CREATED:
        return None
    draft = (await session.execute(
        select(OrderDraft).where(OrderDraft.conversation_id == conversation.id, OrderDraft.status == DRAFT_SUBMITTED,
                                 OrderDraft.mybill_order_no.is_not(None))
        .order_by(OrderDraft.updated_at.desc()).limit(1))).scalars().first()
    if draft is None or draft.updated_at is None:
        return None
    age = now - draft.updated_at
    sent = await _followups_sent(session, conversation.id, (PAYMENT,), draft.updated_at)
    hours = settings.followup_payment_hour_list
    if sent < len(hours) and age >= timedelta(hours=hours[sent]) and age < timedelta(days=2) and is_daytime(now):
        return draft
    return None


async def _send_payment(conversation: Conversation, messenger: WorkerMessenger, mybill: Any, draft: OrderDraft) -> str:
    """'sent', 'paid' (nothing to do) or 'failed'."""
    order_no = draft.mybill_order_no
    try:
        order = await mybill.get_order(order_no)
        if order.get("status") not in ("DRAFT", "PAYMENT_PENDING"):
            return "paid"
        link = await orders.payment_link(mybill, order_no)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Payment reminder skipped", extra={"fields": {"order_no": order_no, "error": repr(exc)[:200]}})
        return "failed"
    if not link or not link.get("short_url"):
        return "failed"
    text = (f"{_hi(conversation)} Your order {order_no} ({format_rupees(order.get('total'))}) is waiting for payment 🙏\n"
            f"Pay securely here: {link['short_url']}\nNeed any help? Just reply here.")
    return "sent" if await messenger.send_text_meta(text, {"followup": PAYMENT, "order_no": order_no}) else "failed"


async def _candidates(session, now: datetime) -> list:
    """One query for the chats that could be due now (the per-chat checks run only for these):
    her last message 12-24 h ago and we spoke after it (enquiry), or an order created in the last 2 days (payment);
    always inside the free window and not with the team."""
    answered = (select(Message.id).where(Message.conversation_id == Conversation.id, Message.direction != INBOUND,
                                         Message.created_at > Conversation.last_customer_message_at).exists())
    open_order = (select(OrderDraft.id).where(OrderDraft.conversation_id == Conversation.id,
                                              OrderDraft.status == DRAFT_SUBMITTED,
                                              OrderDraft.updated_at >= now - timedelta(days=2)).exists())
    window_open = Conversation.last_customer_message_at > now - WINDOW + MARGIN
    stmt = select(Conversation.id).where(
        Conversation.channel == "whatsapp", Conversation.needs_human.is_(False), window_open,
        ((Conversation.last_customer_message_at <= now - EARLIEST) & answered
         & (Conversation.stage != flow.ORDER_CREATED))
        | ((Conversation.stage == flow.ORDER_CREATED) & open_order))
    return list((await session.execute(stmt)).scalars().all())


async def _claim(session, key: str, kind: str, order_no: str | None = None) -> bool:
    """One attempt per reminder: False if this reminder was already tried (sent or refused by WhatsApp)."""
    return (await session.execute(
        pg_insert(ProcessedEvent).values(event_id=key, event_type=f"followup.{kind}", order_no=order_no)
        .on_conflict_do_nothing(index_elements=["event_id"]).returning(ProcessedEvent.event_id)
    )).scalar_one_or_none() is not None


async def run(now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """One pass over recently active WhatsApp chats. Each chat is handled under its conversation lock.
    dry_run: only report who is due (nothing is sent or recorded)."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    stats: dict[str, Any] = {"checked": 0, ENQUIRY: 0, ENQUIRY_LAST: 0, PAYMENT: 0, "skipped": 0, "due": []}
    if not settings.followups_enabled:
        return stats
    if not is_daytime(now):
        return stats                             # reminders only 09:00-21:00 IST
    mybill = mybill_client.get_client()
    staff = set(settings.staff_alert_number_list)
    async with get_sessionmaker()() as session:
        ids = await _candidates(session, now)
    stats["candidates"] = len(ids)
    started = time.monotonic()
    for conversation_id in ids:
        if time.monotonic() - started > RUN_BUDGET_SECONDS:
            stats["stopped_early"] = True        # the next run (30 min later) continues
            break
        async with get_sessionmaker()() as session, session.begin():
            await repo.lock_conversation(session, conversation_id)
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None or conversation.needs_human or conversation.external_user_id in staff:
                continue
            stats["checked"] += 1
            if not in_free_window(conversation, now):
                continue
            messenger = WorkerMessenger(session, conversation)
            ctx = tools.ToolContext(conversation=conversation, mybill=mybill, messenger=messenger,
                                    store=WorkerStore(session), draft=None)
            draft = await _payment(session, conversation, now, settings)
            if dry_run:
                kind = PAYMENT if draft is not None else await _enquiry(session, conversation, now, mybill, settings)
                if kind:
                    stats["due"].append({"customer": conversation.customer_name, "kind": kind,
                                         "design": conversation.current_product_code,
                                         "quiet_hours": round((now - conversation.last_customer_message_at).total_seconds() / 3600, 1)})
                await session.rollback()
                continue
            if draft is not None and mybill is not None:
                n = await _followups_sent(session, conversation_id, (PAYMENT,), draft.updated_at)
                if not await _claim(session, f"followup:{PAYMENT}:{draft.mybill_order_no}:{n}", PAYMENT, draft.mybill_order_no):
                    continue
                outcome = await _send_payment(conversation, messenger, mybill, draft)
                if outcome == "sent":
                    stats[PAYMENT] += 1
                    logger.info("FOLLOWUP_SENT", extra={"fields": {"kind": PAYMENT, "conversation_id": str(conversation_id)}})
                elif outcome == "failed":
                    stats["skipped"] += 1
                continue
            kind = await _enquiry(session, conversation, now, mybill, settings)
            if kind is None:
                continue
            key = f"followup:{kind}:{conversation_id}:{conversation.last_customer_message_at.isoformat()}"
            if not await _claim(session, key, kind):
                continue
            if await _send_enquiry(conversation, messenger, ctx, kind):
                stats[kind] += 1
                logger.info("FOLLOWUP_SENT", extra={"fields": {"kind": kind, "conversation_id": str(conversation_id)}})
            else:
                # e.g. WhatsApp refused a normal message after 24 h; the claim stops a retry.
                stats["skipped"] += 1
                logger.info("FOLLOWUP_NOT_SENT", extra={"fields": {"kind": kind, "error": messenger.error}})
    return stats

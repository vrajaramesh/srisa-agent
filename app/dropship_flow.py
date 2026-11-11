"""Customer side of WhatsApp-forwarded dropship collections (mybill phase 6), as plain code before the AI:

- A DROPSHIP collection code (or one of its item codes) in her message, or the ad / post that brought her: intro
  (title, description, price + shipping, "N colours available"), the collage, then "Reply with a number (e.g. 07) to
  see it up close, or type ALL to see every photo".
- "ALL": every AVAILABLE variant's stamped photo, captioned "07 – Bottle green / red Kalamkari blouse".
- A number (07) or a full code (MGP1111-07): that photo, then the buttons 🛒 Buy this / 🔙 See others.
- 🛒 Buy this: mybill asks the owner(s) "Is MGP1111-07 available with {supplier}?" [Yes] [No]; she is told we're checking.
- The owner's Yes: the variant goes into her cart and the normal order flow continues (address → summary → Confirm →
  order + Razorpay link, delivery = the supplier's courier charge for her pincode). No: SOLD_OUT in mybill, a polite
  message and 3 other available colours.

Everything else (questions, "how much", addresses...) is left to the AI agent, which knows the collection from the
facts. All messages are normal replies inside her 24-hour window.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app import mybill_client
from app.agent import flow, orders, tools
from app.agent.formatting import format_rupees
from app.models import Conversation, Message

logger = logging.getLogger(__name__)

BUY, OTHERS = "dsbuy:", "dsothers:"
NUMBER = re.compile(r"^\s*#?\s*(\d{1,3})\s*$")
ALL = re.compile(r"(?i)^\s*(all|show all|all photos|all colou?rs|send all)\s*[.!]*\s*$")
STATE_TTL = timedelta(hours=24)
CHECKING_TTL = timedelta(minutes=45)
PHOTO_DELAY_SECONDS = 0.6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_forwarded_dropship(collection: dict[str, Any] | None) -> bool:
    return bool(collection) and collection.get("type") == "DROPSHIP" and bool(collection.get("items"))


def _state(conversation: Conversation) -> dict[str, Any]:
    st = dict(conversation.dropship_state or {})
    try:
        at = datetime.fromisoformat(st.get("at") or "")
    except ValueError:
        return {}
    return st if _now() - at <= STATE_TTL else {}


def _save(conversation: Conversation, **changes: Any) -> None:
    st = {**_state(conversation), **changes, "at": _now().isoformat()}
    conversation.dropship_state = st


def _available(collection: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in collection.get("items") or [] if i.get("available") and i.get("image_url")]


def _variant_label(item: dict[str, Any]) -> str:
    code = item.get("item_code") or ""
    return code.rsplit("-", 1)[-1] if "-" in code else code


def caption(item: dict[str, Any]) -> str:
    parts = [p for p in (item.get("colour"), item.get("secondary_desc")) if p]
    return f"{_variant_label(item)} – {' / '.join(parts)}" if parts else _variant_label(item)


def price_line(collection: dict[str, Any]) -> str:
    prices = {i.get("price") for i in collection.get("items") or [] if i.get("price") is not None}
    price = format_rupees(min(prices)) if prices else None
    if price is None:
        return ""
    return f"{price}" + (" + shipping" if collection.get("dispatch_days") is not None else "")


# ── messages ─────────────────────────────────────────────────────────────────

async def send_intro(ctx: tools.ToolContext, collection: dict[str, Any]) -> bool:
    """Intro, collage, then how to choose. False if WhatsApp failed."""
    available = _available(collection)
    lines = [f"*{collection['name']}*"]
    if collection.get("description"):
        lines.append(collection["description"])
    if price_line(collection):
        lines.append(f"💰 {price_line(collection)}")
    lines.append(f"🎨 {len(available)} colour{'s' if len(available) != 1 else ''} available")
    if not await ctx.messenger.send_text("\n".join(lines)):
        return False
    if collection.get("collage_url"):
        if not await ctx.messenger.send_image(collection["collage_url"], f"{collection['name']} – {collection['code']}"):
            return False
    example = _variant_label(available[0]) if available else "01"
    if not await ctx.messenger.send_text(f"Reply with a number (e.g. *{example}*) to see it up close, or type *ALL* to see every photo."):
        return False
    ctx.conversation.current_collection_code = collection["code"]
    _save(ctx.conversation, collection=collection["code"], step="browsing")
    ctx.actions.append({"action": "dropship_intro", "code": collection["code"]})
    return True


async def send_all(ctx: tools.ToolContext, collection: dict[str, Any]) -> bool:
    items = _available(collection)
    for i, item in enumerate(items):
        if not await ctx.messenger.send_image(item["image_url"], caption(item)):
            return False
        if i < len(items) - 1:
            await asyncio.sleep(PHOTO_DELAY_SECONDS)   # keeps WhatsApp's order and avoids bursts
    ok = await ctx.messenger.send_text("Reply with the number you like (e.g. *07*) to buy it 😊")
    ctx.actions.append({"action": "dropship_all", "code": collection["code"], "photos": len(items)})
    return ok


async def send_item(ctx: tools.ToolContext, collection: dict[str, Any], item: dict[str, Any]) -> bool:
    if not item.get("available"):
        return await ctx.messenger.send_text(f"Sorry, {item['item_code']} is sold out 😔 Reply *ALL* to see the colours available.")
    if not await ctx.messenger.send_image(item["image_url"], f"{item['item_code']} · {caption(item)}"
                                          + (f" · {price_line(collection)}" if price_line(collection) else "")):
        return False
    ok = await ctx.messenger.send_buttons(
        f"Would you like {item['item_code']}?",
        [{"id": f"{BUY}{item['item_code']}", "title": "🛒 Buy this"},
         {"id": f"{OTHERS}{collection['code']}", "title": "🔙 See others"}])
    _save(ctx.conversation, collection=collection["code"], step="browsing", item=item["item_code"])
    ctx.actions.append({"action": "dropship_item", "code": item["item_code"]})
    return ok


async def buy(ctx: tools.ToolContext, item_code: str) -> bool:
    """Ask the owner(s) whether it's available; tell her we're checking."""
    conv = ctx.conversation
    if item_code in (_state(conv).get("confirmed") or []):
        return await add_to_cart(ctx, item_code)
    try:
        await ctx.mybill.dropship_ask_availability(item_code, str(conv.id), conv.customer_name, conv.external_user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Availability question not sent", extra={"fields": {"item": item_code, "error": repr(exc)[:200]}})
        return await ctx.messenger.send_text("Sorry, something went wrong on our side – please try again in a minute 🙏")
    _save(conv, step="awaiting_owner", item=item_code, asked_at=_now().isoformat())
    ctx.actions.append({"action": "dropship_availability_asked", "code": item_code})
    return await ctx.messenger.send_text(f"Great choice! 😊 Let me quickly confirm {item_code} is in stock – "
                                         "I'll message you in a few minutes 🙏")


async def add_to_cart(ctx: tools.ToolContext, item_code: str) -> bool:
    """Owner confirmed: one piece in her cart, then the normal flow (address → summary)."""
    result = await tools.set_cart_item(ctx, {"code": item_code, "quantity": 1})
    if result.get("error"):
        logger.warning("Dropship cart failed", extra={"fields": {"item": item_code, "error": result}})
        return await ctx.messenger.send_text(f"✅ Good news – {item_code} is available! Our team will help you place the order shortly 🙏")
    done = await tools.finish_cart(ctx, {})
    if done.get("stage") == flow.CONFIRM:   # her address is already saved: straight to the summary
        summary = await tools.send_order_summary(ctx, {})
        return not summary.get("error")
    return await ctx.messenger.send_text(
        f"✅ Good news – {item_code} is available!\nPlease send your *name, full address and pincode* "
        "and I'll send the order summary with the delivery charge.")


# ── the worker's entry point ─────────────────────────────────────────────────

async def path(ctx: tools.ToolContext, pending: list[Message]) -> str | None:
    """'handled' if a dropship step answered her, an error string (retry), or None (the AI continues)."""
    if ctx.mybill is None or ctx.conversation.channel != "whatsapp":
        return None
    conv = ctx.conversation
    last = pending[-1]
    reply_id = last.reply_id or ""
    text = (last.text or "").strip()
    st = _state(conv)

    if reply_id.startswith(BUY):
        return _ok(ctx, await buy(ctx, reply_id[len(BUY):]))
    if reply_id.startswith(OTHERS):
        collection = await _collection(ctx, reply_id[len(OTHERS):])
        return _ok(ctx, await send_intro(ctx, collection)) if collection else None

    # a dropship code in her message (collection or variant)
    from app.agent import resolution
    for code in reversed([c for m in pending if not m.reply_id for c in resolution.find_collection_codes(m.text)]):
        collection, item_code = await _collection_or_item(ctx, code)
        if collection is None:
            continue
        if item_code:
            item = _find(collection, item_code)
            return _ok(ctx, await send_item(ctx, collection, item)) if item else None
        return _ok(ctx, await send_intro(ctx, collection))

    if st.get("collection") and st.get("step") in ("browsing", "awaiting_owner"):
        collection = await _collection(ctx, st["collection"])
        if collection is None:
            return None
        if ALL.match(text):
            return _ok(ctx, await send_all(ctx, collection))
        if m := NUMBER.match(text):
            item = _find(collection, f"{collection['code']}-{int(m.group(1)):02d}")
            if item:
                return _ok(ctx, await send_item(ctx, collection, item))
            return _ok(ctx, await ctx.messenger.send_text(
                f"I couldn't find number {m.group(1)} – reply *ALL* to see every colour with its number."))
        if st.get("step") == "awaiting_owner":
            try:
                asked = datetime.fromisoformat(st.get("asked_at") or "")
            except ValueError:
                asked = _now() - CHECKING_TTL
            if _now() - asked < CHECKING_TTL and not reply_id:
                return _ok(ctx, await ctx.messenger.send_text(
                    f"Still checking {st.get('item')} for you – I'll message you as soon as I hear back 🙏"))
    return None


def _ok(ctx: tools.ToolContext, sent: bool) -> str:
    return "handled" if sent else (ctx.messenger.error or "send failed")


def _find(collection: dict[str, Any], item_code: str) -> dict[str, Any] | None:
    return next((i for i in collection.get("items") or [] if i["item_code"].upper() == item_code.upper()), None)


async def _collection(ctx: tools.ToolContext, code: str) -> dict[str, Any] | None:
    collection, _ = await _collection_or_item(ctx, code)
    return collection


async def _collection_or_item(ctx: tools.ToolContext, code: str) -> tuple[dict[str, Any] | None, str | None]:
    """An active DROPSHIP collection by collection or item code (+ the item code when it was one)."""
    try:
        collection, item_code = await tools._load_collection(ctx, code)
    except Exception:  # noqa: BLE001 - mybill down: the AI path reports it
        return None, None
    if not is_forwarded_dropship(collection) or not collection.get("active", True):
        return None, None
    return collection, item_code


# ── the owner's answer (a tap on Yes / No, from dropship_intake) ─────────────

async def owner_answer(reply_id: str, owner: str) -> dict[str, Any]:
    """reply_id avail:{y|n}:{conversationId}:{itemCode}."""
    from app import repositories as repo, whatsapp_client
    from app.db import get_sessionmaker
    from app.worker import WorkerMessenger, WorkerStore

    try:
        _, answer, conv_id, item_code = reply_id.split(":", 3)
        conversation_id = uuid.UUID(conv_id)
    except ValueError:
        return {"handled": False, "reason": "bad_reply_id"}
    async with get_sessionmaker()() as session, session.begin():
        await repo.lock_conversation(session, conversation_id)
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            return {"handled": False, "reason": "unknown_conversation"}
        st = _state(conv)
        if st.get("step") != "awaiting_owner" or (st.get("item") or "").upper() != item_code.upper():
            await whatsapp_client.send_text(owner, f"👍 {item_code}: already answered (or she chose another one).")
            return {"handled": True, "action": "stale"}
        base = mybill_client.get_client()
        mybill = mybill_client.FirmClient(base, conv) if base is not None else None
        ctx = tools.ToolContext(conversation=conv, mybill=mybill, messenger=WorkerMessenger(session, conv),
                                store=WorkerStore(session), draft=await repo.open_draft(session, conversation_id))
        if answer == "y":
            _save(conv, step="confirmed", confirmed=sorted({*(st.get("confirmed") or []), item_code}))
            sent = await add_to_cart(ctx, item_code)
            await whatsapp_client.send_text(owner, f"👍 {item_code}: customer told it's available"
                                            + ("" if sent else " – but WhatsApp couldn't reach her (24 h window?)"))
            return {"handled": True, "action": "confirmed", "sent": sent}
        try:
            await ctx.mybill.dropship_sold_out(item_code)
        except Exception as exc:  # noqa: BLE001
            logger.error("Sold-out not saved", extra={"fields": {"item": item_code, "error": repr(exc)[:200]}})
        collection = await _collection(ctx, item_code)
        others = [i for i in _available(collection or {}) if i["item_code"].upper() != item_code.upper()][:3]
        _save(conv, step="browsing", item=None)
        sent = await ctx.messenger.send_text(
            f"Sorry 😔 {item_code} has just sold out." + (" Here are some colours still available:" if others else
                                                          " Reply *ALL* to see what's still available."))
        for item in others:
            sent = sent and await ctx.messenger.send_image(item["image_url"], caption(item))
        if others:
            sent = sent and await ctx.messenger.send_text("Reply with the number you like (e.g. "
                                                          f"*{_variant_label(others[0])}*) or *ALL* to see every colour.")
        await whatsapp_client.send_text(owner, f"👍 {item_code}: marked SOLD OUT and the customer was offered other colours"
                                        + ("" if sent else " – but WhatsApp couldn't reach her"))
        return {"handled": True, "action": "sold_out", "sent": sent}


def facts(conversation: Conversation) -> list[str]:
    """Lines for the AI's facts block while a dropship purchase is in progress."""
    st = _state(conversation)
    if st.get("step") == "awaiting_owner":
        return [f"- Dropship: she tapped Buy on {st.get('item')}; we are checking availability with our team. "
                "Don't add it to the cart or ask for payment yet - tell her we'll confirm shortly."]
    if st.get("confirmed"):
        return [f"- Dropship: availability confirmed for {', '.join(st['confirmed'])} - continue her order "
                "(address → summary)."]
    return []


def unconfirmed_dropship(conversation: Conversation, quote_lines: list[dict[str, Any]]) -> list[str]:
    """Dropship cart lines the owner hasn't confirmed yet (they must not reach the payment link)."""
    confirmed = {c.upper() for c in (_state(conversation).get("confirmed") or [])}
    return [l.get("item_code") for l in quote_lines
            if l.get("kind") == "DROPSHIP" and (l.get("item_code") or "").upper() not in confirmed]

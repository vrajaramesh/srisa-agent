"""Customer side of WhatsApp-forwarded dropship collections (mybill phase 6), as plain code before the AI:

- A DROPSHIP collection code (or one of its item codes) in her message, or the ad / post that brought her: intro
  (title, description, price + shipping, "N colours available"), then every AVAILABLE variant as its own photo (the
  code is stamped on it; caption = the code, e.g. MGP1111-07 - no collage), then "Reply with the number (e.g. 07)".
- "ALL": the same photos again.
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
    """Just the code (MGP1111-07) - it is stamped on the photo too."""
    return item.get("item_code") or ""


def price_line(collection: dict[str, Any]) -> str:
    prices = {i.get("price") for i in collection.get("items") or [] if i.get("price") is not None}
    price = format_rupees(min(prices)) if prices else None
    if price is None:
        return ""
    return f"{price}" + (" + shipping" if collection.get("dispatch_days") is not None else "")


# ── messages ─────────────────────────────────────────────────────────────────

async def send_intro(ctx: tools.ToolContext, collection: dict[str, Any]) -> bool:
    """Intro, every available colour as its own photo with its code, then how to choose. False if WhatsApp failed."""
    available = _available(collection)
    lines = [f"*{collection['name']}*"]
    if collection.get("description"):
        lines.append(collection["description"])
    if price_line(collection):
        lines.append(f"💰 {price_line(collection)}")
    lines.append(f"🎨 {len(available)} colour{'s' if len(available) != 1 else ''} available")
    if not await ctx.messenger.send_text("\n".join(lines)):
        return False
    ctx.conversation.current_collection_code = collection["code"]
    _save(ctx.conversation, collection=collection["code"], step="browsing")
    ctx.actions.append({"action": "dropship_intro", "code": collection["code"]})
    return await send_all(ctx, collection)


async def send_all(ctx: tools.ToolContext, collection: dict[str, Any]) -> bool:
    items = _available(collection)
    for i, item in enumerate(items):
        if not await ctx.messenger.send_image(item["image_url"], caption(item)):
            return False
        if i < len(items) - 1:
            await asyncio.sleep(PHOTO_DELAY_SECONDS)   # keeps WhatsApp's order and avoids bursts
    example = _variant_label(items[0]) if items else "01"
    ok = await ctx.messenger.send_text(f"Reply with the number of the one you like (e.g. *{example}*) to buy it 😊")
    ctx.actions.append({"action": "dropship_all", "code": collection["code"], "photos": len(items)})
    return ok


async def send_item(ctx: tools.ToolContext, collection: dict[str, Any], item: dict[str, Any]) -> bool:
    if not item.get("available"):
        return await ctx.messenger.send_text(f"Sorry, {item['item_code']} is sold out 😔 Reply *ALL* to see the colours available.")
    if not await ctx.messenger.send_image(item["image_url"], caption(item)
                                          + (f" · {price_line(collection)}" if price_line(collection) else "")):
        return False
    ok = await ctx.messenger.send_buttons(
        f"Would you like {item['item_code']}?",
        [{"id": f"{BUY}{item['item_code']}", "title": "🛒 Buy this"},
         {"id": f"{OTHERS}{collection['code']}", "title": "🔙 See others"}])
    _save(ctx.conversation, collection=collection["code"], step="browsing", item=item["item_code"])
    ctx.actions.append({"action": "dropship_item", "code": item["item_code"]})
    return ok


def _pending(st: dict[str, Any]) -> dict[str, int]:
    """Variants waiting for the owner's Yes / No: {CODE: pieces} (older states kept one "item")."""
    if st.get("pending"):
        return {k.upper(): int(v) for k, v in st["pending"].items()}
    if st.get("step") == "awaiting_owner" and st.get("item"):
        return {st["item"].upper(): 1}
    return {}


def _qty_text(code: str, qty: int) -> str:
    return f"{code} × {qty}" if qty > 1 else code


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


async def _ask_owner(ctx: tools.ToolContext, item_code: str, qty: int) -> bool:
    """mybill asks the collection's owner "Is X available (n pieces)?"; False if that failed."""
    conv = ctx.conversation
    try:
        await ctx.mybill.dropship_ask_availability(item_code, str(conv.id), conv.customer_name, conv.external_user_id, qty)
    except Exception as exc:  # noqa: BLE001
        logger.error("Availability question not sent", extra={"fields": {"item": item_code, "error": repr(exc)[:200]}})
        return False
    st = _state(conv)
    _save(conv, step="awaiting_owner", pending={**_pending(st), item_code.upper(): qty}, item=None,
          asked_at=_now().isoformat())
    ctx.actions.append({"action": "dropship_availability_asked", "code": item_code, "quantity": qty})
    return True


async def buy(ctx: tools.ToolContext, item_code: str, qty: int = 1) -> bool:
    """Ask the owner whether it's available; tell her we're checking."""
    return await buy_many(ctx, [(item_code.upper(), qty)])


async def buy_many(ctx: tools.ToolContext, picks: list[tuple[str, int]]) -> bool:
    """She wants these variants (code, pieces): already confirmed ones go straight to the cart, the others are asked."""
    confirmed = {c.upper() for c in _state(ctx.conversation).get("confirmed") or []}
    asked, added = [], []
    for code, qty in picks:
        if code in confirmed:
            if await _cart_add(ctx, code, qty):
                added.append(_qty_text(code, qty))
            continue
        if not await _ask_owner(ctx, code, qty):
            return await ctx.messenger.send_text("Sorry, something went wrong on our side – please try again in a minute 🙏")
        asked.append(_qty_text(code, qty))
    if asked:
        return await ctx.messenger.send_text(f"Great choice! 😊 Let me quickly confirm {_join(asked)} "
                                             f"{'is' if len(asked) == 1 else 'are'} in stock – I'll message you in a few minutes 🙏")
    return await _proceed(ctx, f"✅ {_join(added)} added to your order.") if added else True


async def _cart_add(ctx: tools.ToolContext, item_code: str, qty: int) -> bool:
    result = await tools.set_cart_item(ctx, {"code": item_code, "quantity": qty})
    if result.get("error"):
        logger.warning("Dropship cart failed", extra={"fields": {"item": item_code, "error": result}})
        return False
    return True


async def _proceed(ctx: tools.ToolContext, lead: str) -> bool:
    """Nothing left to check: the normal flow (address → summary)."""
    done = await tools.finish_cart(ctx, {})
    if done.get("error"):
        return await ctx.messenger.send_text(f"{lead}\nOur team will help you place the order shortly 🙏")
    if done.get("stage") == flow.CONFIRM:   # her address is already saved: straight to the summary
        summary = await tools.send_order_summary(ctx, {})
        return not summary.get("error")
    return await ctx.messenger.send_text(
        f"{lead}\nPlease send your *name, full address and pincode* and I'll send the order summary with the delivery charge.")


async def add_to_cart(ctx: tools.ToolContext, item_code: str, qty: int = 1) -> bool:
    """Owner confirmed: the pieces in her cart, then the normal flow (address → summary)."""
    if not await _cart_add(ctx, item_code, qty):
        return await ctx.messenger.send_text(f"✅ Good news – {item_code} is available! Our team will help you place the order shortly 🙏")
    return await _proceed(ctx, f"✅ Good news – {_qty_text(item_code, qty)} {'is' if qty == 1 else 'are'} available!")


# Her choice in one message: "07-2 sarees, 01-1 saree", "07 x 2 and 01", "KSS1211-07 2", "07, 09".
_PICK = re.compile(
    r"^\s*(?:([A-Z]{2,}[A-Z0-9]*?\d+[A-Z]?)\s*-\s*)?#?(\d{1,3})"
    r"(?:\s*(?:-|x|×|\*|:|=|–)\s*(\d{1,2})|\s+(\d{1,2}))?"
    r"\s*(sarees?|saris?|pcs?|pieces?|nos?|qty|suits?|sets?)?\s*\.?\s*$", re.IGNORECASE)
_SPLIT = re.compile(r"\s*(?:,|;|\n|&|\band\b|\+)\s*", re.IGNORECASE)
MAX_PIECES = 20


def parse_picks(text: str, collection: dict[str, Any]) -> list[tuple[str, int, bool]] | None:
    """[(ITEM CODE, pieces, pieces given)] if the whole message is a list of numbers / codes with optional pieces,
    else None. Codes of another collection don't count."""
    parts = [p for p in _SPLIT.split(text or "") if p.strip()]
    if not parts:
        return None
    out = []
    for part in parts:
        m = _PICK.match(part)
        if not m:
            return None
        prefix, num, qty = m.group(1), int(m.group(2)), m.group(3) or m.group(4)
        if m.group(5) and not qty:   # "2 sarees": a count, not a colour number - the AI asks which
            return None
        if prefix and prefix.upper() != str(collection["code"]).upper():
            return None
        pieces = int(qty) if qty else 1
        if not 1 <= pieces <= MAX_PIECES:
            return None
        out.append((f"{collection['code']}-{num:02d}".upper(), pieces, qty is not None))
    return out


async def choose(ctx: tools.ToolContext, collection: dict[str, Any], picks: list[tuple[str, int, bool]]) -> bool:
    """One number without pieces: show it with Buy. Several, or with pieces: ask the owner for all of them."""
    if len(picks) == 1 and not picks[0][2]:
        item = _find(collection, picks[0][0])
        if item:
            return await send_item(ctx, collection, item)
        return await ctx.messenger.send_text(
            f"I couldn't find number {picks[0][0].rsplit('-', 1)[-1]} – reply *ALL* to see every colour with its number.")
    wanted: dict[str, int] = {}
    missing, sold = [], []
    for code, qty, _ in picks:
        item = _find(collection, code)
        if item is None:
            missing.append(code.rsplit("-", 1)[-1])
        elif not item.get("available"):
            sold.append(code)
        else:
            wanted[code] = wanted.get(code, 0) + qty
    notes = []
    if missing:
        notes.append(f"I couldn't find number{'s' if len(missing) > 1 else ''} {', '.join(missing)}")
    if sold:
        notes.append(f"{_join(sold)} {'is' if len(sold) == 1 else 'are'} sold out 😔")
    if notes and not await ctx.messenger.send_text(" · ".join(notes) + (" – reply *ALL* to see what's available." if not wanted else "")):
        return False
    if not wanted:
        return True
    _save(ctx.conversation, collection=collection["code"])
    return await buy_many(ctx, list(wanted.items()))


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
            if (picks := parse_picks(text, collection)) and (len(picks) > 1 or picks[0][2]):   # codes with pieces
                return _ok(ctx, await choose(ctx, collection, picks))
            item = _find(collection, item_code)
            return _ok(ctx, await send_item(ctx, collection, item)) if item else None
        return _ok(ctx, await send_intro(ctx, collection))

    if st.get("collection") and st.get("step") in ("browsing", "awaiting_owner"):
        collection = await _collection(ctx, st["collection"])
        if collection is None:
            return None
        if ALL.match(text):
            return _ok(ctx, await send_all(ctx, collection))
        if not reply_id and (picks := parse_picks(text, collection)):   # "07", "07-2 sarees, 01-1 saree"
            return _ok(ctx, await choose(ctx, collection, picks))
        if st.get("step") == "awaiting_owner":
            try:
                asked = datetime.fromisoformat(st.get("asked_at") or "")
            except ValueError:
                asked = _now() - CHECKING_TTL
            if _now() - asked < CHECKING_TTL and not reply_id:
                return _ok(ctx, await ctx.messenger.send_text(
                    f"Still checking {_join(list(_pending(st)) or ['it'])} for you – I'll message you as soon as I hear back 🙏"))
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
        pending = _pending(st)
        code = item_code.upper()
        if code not in pending:
            await whatsapp_client.send_text(owner, f"👍 {item_code}: already answered (or she chose another one).")
            return {"handled": True, "action": "stale"}
        qty = pending.pop(code)
        base = mybill_client.get_client()
        mybill = mybill_client.FirmClient(base, conv) if base is not None else None
        ctx = tools.ToolContext(conversation=conv, mybill=mybill, messenger=WorkerMessenger(session, conv),
                                store=WorkerStore(session), draft=await repo.open_draft(session, conversation_id))
        still = f"\nStill checking {_join(list(pending))} – I'll message you soon 🙏" if pending else ""
        if answer == "y":
            confirmed = sorted({*(st.get("confirmed") or []), code})
            _save(conv, step="awaiting_owner" if pending else "confirmed", pending=pending, item=None, confirmed=confirmed)
            if pending:
                added = await _cart_add(ctx, code, qty)
                sent = await ctx.messenger.send_text(f"✅ {_qty_text(code, qty)} {'is' if qty == 1 else 'are'} available"
                                                     + (" and added to your order." if added else ".") + still)
            else:
                sent = await add_to_cart(ctx, code, qty)
            await whatsapp_client.send_text(owner, f"👍 {_qty_text(code, qty)}: customer told it's available"
                                            + ("" if sent else " – but WhatsApp couldn't reach her (24 h window?)"))
            return {"handled": True, "action": "confirmed", "sent": sent}
        try:
            await ctx.mybill.dropship_sold_out(code)
        except Exception as exc:  # noqa: BLE001
            logger.error("Sold-out not saved", extra={"fields": {"item": code, "error": repr(exc)[:200]}})
        _save(conv, step="awaiting_owner" if pending else "browsing", pending=pending, item=None)
        if pending:   # other colours still being checked
            sent = await ctx.messenger.send_text(f"Sorry 😔 {code} has just sold out.{still}")
        elif any(c != code for c in st.get("confirmed") or []) and draft_has_dropship(ctx):
            sent = await _proceed(ctx, f"Sorry 😔 {code} has just sold out – I've kept the others in your order.")
        else:
            collection = await _collection(ctx, code)
            others = [i for i in _available(collection or {}) if i["item_code"].upper() != code][:3]
            sent = await ctx.messenger.send_text(
                f"Sorry 😔 {code} has just sold out." + (" Here are some colours still available:" if others else
                                                         " Reply *ALL* to see what's still available."))
            for item in others:
                sent = sent and await ctx.messenger.send_image(item["image_url"], caption(item))
            if others:
                sent = sent and await ctx.messenger.send_text("Reply with the number you like (e.g. "
                                                              f"*{_variant_label(others[0])}*) or *ALL* to see every colour.")
        await whatsapp_client.send_text(owner, f"👍 {code}: marked SOLD OUT and the customer was told"
                                        + ("" if sent else " – but WhatsApp couldn't reach her"))
        return {"handled": True, "action": "sold_out", "sent": sent}


def draft_has_dropship(ctx: tools.ToolContext) -> bool:
    """Her cart already has confirmed dropship lines."""
    return any((line.get("kind") == "DROPSHIP") for line in (ctx.draft.lines if ctx.draft else None) or [])


def facts(conversation: Conversation) -> list[str]:
    """Lines for the AI's facts block while a dropship purchase is in progress."""
    st = _state(conversation)
    if st.get("step") == "awaiting_owner" and (pending := _pending(st)):
        return [f"- Dropship: we are checking availability with our team for "
                f"{', '.join(_qty_text(c, q) for c, q in pending.items())}. Don't add those to the cart or ask for payment "
                "yet - tell her we'll confirm shortly."
                + (f" Already confirmed: {', '.join(st['confirmed'])}." if st.get("confirmed") else "")]
    if st.get("confirmed"):
        return [f"- Dropship: availability confirmed for {', '.join(st['confirmed'])} - continue her order "
                "(address → summary)."]
    return []


def unconfirmed_dropship(conversation: Conversation, quote_lines: list[dict[str, Any]]) -> list[str]:
    """Dropship cart lines the owner hasn't confirmed yet (they must not reach the payment link)."""
    confirmed = {c.upper() for c in (_state(conversation).get("confirmed") or [])}
    return [l.get("item_code") for l in quote_lines
            if l.get("kind") == "DROPSHIP" and (l.get("item_code") or "").upper() not in confirmed]

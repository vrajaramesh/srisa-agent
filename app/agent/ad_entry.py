"""Click-to-WhatsApp ad visitors: deterministic first reply, the ad:* buttons, and the marketing event.

First reply (no model call) when a message carries a referral, its design was resolved and the conversation is
new (or quiet for 7+ days): the design's first photo with price + stitching time, then three reply buttons.
A greeting line is added only if the ad had no welcome_message (she already saw that one).
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent import tools
from app.agent.formatting import format_rupees
from app.agent.resolution import mybill_source_type
from app.models import Conversation, Message
from app.webhook_parser import Referral, parse_referral

logger = logging.getLogger(__name__)

FRESH_START_GAP = timedelta(days=7)
# Meta's default ad message ("Hello! Can I get more info on this?"). Some ad taps arrive without a referral;
# this text still tells us she came from an ad, so we assume the ad that brought the latest chats.
AD_GREETING = re.compile(r"^\s*(hello|hi)?[!,.\s]*can i (get|have) more info(rmation)? (on|about) this\s*[?.!]*\s*$",
                         re.IGNORECASE)
INFERRED_AD_WINDOW = timedelta(days=3)


def is_ad_greeting(text: str | None) -> bool:
    return bool(AD_GREETING.match(text or ""))


# The ad's pre-filled order message ("Hi SRISA! I'd like to order FRO-11 🛍️") - the first reply answers it.
_ORDER_PREFILL = re.compile(r"(?i)\bi'?d like to order\b|\bi want to order\b")


def asks_something(message: Message) -> bool:
    """A typed message beyond Meta's greeting / the pre-filled order text (e.g. "Price", "Is it readymade?"),
    which the deterministic first reply doesn't answer. Taps and media don't count."""
    text = (message.text or "").strip()
    return bool(text) and not message.reply_id and message.type == "text" and not is_ad_greeting(text) \
        and not _ORDER_PREFILL.search(text)


def inferred_referral(entry: Any) -> Referral:
    """A Referral rebuilt from the newest real ad entry (no click id / welcome text: we didn't get this tap's)."""
    return Referral(source_id=entry.source_id, source_type=entry.source_type, source_url=entry.source_url,
                    headline=entry.headline, body=entry.body, media_type=entry.media_type, inferred=True)


def referral_of(message: Message) -> Referral | None:
    """The referral stored in an inbound message's raw JSON."""
    return parse_referral((message.raw or {}).get("referral"))


def entry_message(pending: list[Message]) -> tuple[Message, Referral] | tuple[None, None]:
    """The newest pending message that came with a referral."""
    for message in reversed(pending):
        if (referral := referral_of(message)) is not None:
            return message, referral
    return None, None


def message_time(message: Message) -> datetime | None:
    """When she sent it (Meta's unix timestamp in the raw JSON), falling back to when we stored it."""
    try:
        return datetime.fromtimestamp(int((message.raw or {}).get("timestamp")), tz=timezone.utc)
    except (TypeError, ValueError):
        return message.created_at


def is_fresh_start(previous_inbound_at: datetime | None, message: Message) -> bool:
    """First message of the conversation, or the first after 7 quiet days."""
    if previous_inbound_at is None:
        return True
    return (message.created_at or datetime.now(timezone.utc)) - previous_inbound_at > FRESH_START_GAP


def _first_name(name: str | None) -> str:
    return (name or "").strip().split(" ")[0]


MAX_DESCRIPTION = 600


def _description(design: dict[str, Any]) -> str:
    """The design's description from mybill, tidied (paragraphs kept), clipped at a sentence/word boundary."""
    text = "\n".join(line.strip() for line in (design.get("description") or "").strip().splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= MAX_DESCRIPTION:
        return text
    cut = text[:MAX_DESCRIPTION]
    end = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[:end + 1] if end > MAX_DESCRIPTION // 2 else cut.rsplit(" ", 1)[0] + "…").strip()


def caption(design: dict[str, Any], sizes: str | None = None, fabric_price: str | None = None) -> str:
    """The product card: what it is (description), price, stitching time, sizes, fabric-only price."""
    lead = design.get("lead_time_days")
    description = _description(design)
    lines = [f"*{design['name']} ({design['code']})*"]
    if description:
        lines += ["", description, ""]
    lines.append(f"💰 {format_rupees(design.get('base_price'))} – stitched to your size")
    lines.append(f"🧵 Ready in {lead} days, then shipped across India" if lead else "🧵 Shipped across India")
    if sizes:
        lines.append(f"📏 Sizes {sizes}, or a custom size")
    if fabric_price:
        lines.append(f"✂️ Fabric alone also available – {fabric_price}")
    return "\n".join(lines)


def greeting(conversation: Conversation) -> str:
    first = _first_name(conversation.customer_name)
    return f"Hi {first}! 👋 Here are the details 👇" if first else "Hi! 👋 Here are the details 👇"


async def _size_range(ctx: tools.ToolContext) -> str | None:
    """"XS–4XL" from mybill's size chart (None if it can't be read)."""
    try:
        codes = [row["size_code"] for row in await ctx.chart()]
    except Exception:  # noqa: BLE001 - the card is still useful without sizes
        return None
    return f"{codes[0]}–{codes[-1]}" if len(codes) > 1 else (codes[0] if codes else None)


def buttons(code: str) -> list[dict[str, str]]:
    return [{"id": f"ad:order:{code}", "title": "🛍️ Order this"},
            {"id": f"ad:photos:{code}", "title": "📸 More photos"},
            {"id": "ad:sizes", "title": "📏 Size guide"}]


async def send_first_reply(ctx: tools.ToolContext, referral: Referral, design: dict[str, Any]) -> bool:
    """Photo (short caption), then the details card with the Order / Photos / Size guide buttons under it (no
    question - an ad visitor gets the details straight away), then the fabric-only offer.
    False if WhatsApp failed (the worker retries)."""
    details = caption(design, await _size_range(ctx), tools.fabric_price_text(tools.design_fabrics(design)) or None)
    hello = greeting(ctx.conversation)
    images = design.get("images") or []
    if images:
        short = f"{hello.split(' Here are')[0]} Here's {design['code']} – {format_rupees(design.get('base_price'))} 👇"
        if not await ctx.messenger.send_image(images[0]["url"], short):
            return False
        body = details
    else:
        body = f"{hello}\n\n{details}"
    if not await ctx.messenger.send_buttons(body, buttons(design["code"]), {"kind": "ad_entry", "code": design["code"]}):
        return False
    ctx.actions.append({"action": "ad_first_reply", "code": design["code"], "photo": bool(images)})
    # The fabric is also sold alone on the website. A failure here doesn't redo the reply above.
    if not await tools.offer_fabric(ctx, design):
        logger.warning("Fabric offer not sent", extra={"fields": {"code": design["code"], "error": ctx.messenger.error}})
    return True


async def apply_taps(ctx: tools.ToolContext, pending: list[Message]) -> list[dict[str, Any]]:
    """ad:order:<code> -> start_order, ad:photos:<code> -> send_design_photos. ad:sizes is left to the model
    (get_size_chart): the size picker only works once an order is started. The model continues afterwards."""
    applied = []
    for message in pending:
        parts = (message.reply_id or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "ad" or not parts[2]:
            continue
        kind, code = parts[1], parts[2]
        if kind == "order":
            name, args = "start_order", {"design_code": code}
        elif kind == "photos":
            name, args = "send_design_photos", {"code": code}
        else:
            continue
        result, _, _ = await tools.run_tool(ctx, name, args)
        applied.append({"tap": message.reply_id, "tool": name, "error": result.get("error")})
        logger.info("Ad tap applied", extra={"fields": {"reply_id": message.reply_id, "error": result.get("error")}})
    return applied


def marketing_event(conversation: Conversation, referral: Referral, occurred_at: datetime | None) -> dict[str, Any] | None:
    """conversation_started payload for mybill, or None when mybill can't attribute it (unknown type / id)."""
    source_type = mybill_source_type(referral.source_type)
    if not source_type or not (referral.source_id or "").isdigit():
        return None
    event: dict[str, Any] = {"event_type": "conversation_started", "source_type": source_type,
                             "source_id": referral.source_id}
    if referral.ctwa_clid:
        event["ctwa_clid"] = referral.ctwa_clid
    if conversation.mybill_customer_id:
        event["customer_id"] = conversation.mybill_customer_id
    if occurred_at:
        event["occurred_at"] = occurred_at.isoformat()
    return event

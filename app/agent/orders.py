"""Order logic shared by tools, the worker and the CLI: progress, quotes, the summary, and confirmation.

Confirmation is a CODE path (never a model tool): only a tap on confirm:<draft>:<version>, or a short "yes"
right after the summary for the current version, creates the mybill order - exactly once.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from app.agent import flow
from app.agent.formatting import format_rupees
from app.agent.resolution import mybill_source_type
from app.models import DRAFT_OPEN, DRAFT_SUBMITTED, Conversation, Message, OrderDraft
from app.mybill_client import MybillClient, MybillRejected, MybillUnavailable

logger = logging.getLogger(__name__)

SUMMARY_KIND = "order_summary"
AFFIRMATIVE = {"yes", "y", "yes please", "ok", "okay", "k", "confirm", "confirmed", "haan", "ha", "haa", "han",
               "avunu", "avnu", "sare", "done", "👍", "✅", "👌"}
CONFIRM_RE = re.compile(r"^confirm:([0-9a-f-]{36}):(\d+)$")


class Messenger(Protocol):
    """Sends to THIS conversation's customer (the recipient is never a parameter) and records what was sent."""

    async def send_text(self, text: str) -> bool: ...
    async def send_image(self, url: str, caption: str | None) -> bool: ...
    async def send_buttons(self, body: str, buttons: list[dict[str, str]], meta: dict | None = None) -> bool: ...
    async def send_link_button(self, body: str, button_text: str, url: str, image_url: str | None = None) -> bool: ...
    async def send_list(self, body: str, button_text: str, sections: list[dict[str, Any]],
                        meta: dict | None = None) -> bool: ...


class OrderStore(Protocol):
    """Creates and finds drafts (worker: DB session; CLI: memory)."""

    async def new_draft(self, conversation: Conversation) -> OrderDraft: ...
    async def get_draft(self, draft_id: uuid.UUID) -> OrderDraft | None: ...
    async def latest_order_no(self, conversation: Conversation) -> str | None: ...
    async def create_ref_code(self, conversation: Conversation, design_code: str | None) -> str: ...


def phone_e164(conversation: Conversation) -> str:
    """WhatsApp ids are digits with country code; E.164 adds the plus."""
    return "+" + conversation.external_user_id.lstrip("+")


def indian_mobile_e164(value: str | None) -> str | None:
    """A 10-digit Indian mobile (optionally +91 / 91 / 0 prefixed) as +91XXXXXXXXXX, else None."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return f"+91{digits}" if len(digits) == 10 and digits[0] in "6789" else None


def customer_identity(conversation: Conversation, name: str | None = None) -> dict[str, Any]:
    """upsert_customer arguments for this conversation's channel."""
    name = name if name is not None else conversation.customer_name
    if conversation.channel == "instagram":
        return {"instagram_user_id": conversation.external_user_id, "name": name}
    return {"phone_e164": phone_e164(conversation), "name": name}


def option_maps(design: dict[str, Any]) -> tuple[set[str], dict[int, str], dict[int, dict[str, Any]]]:
    """(groups, option_id -> group, option_id -> option) for a mybill design (active options only)."""
    groups: set[str] = set()
    by_group: dict[int, str] = {}
    options: dict[int, dict[str, Any]] = {}
    for g in design.get("option_groups", []):
        groups.add(g["group"])
        for o in g["options"]:
            by_group[o["option_id"]] = g["group"]
            options[o["option_id"]] = {**o, "group": g["group"]}
    return groups, by_group, options


def fabric_product_id(draft: OrderDraft) -> int:
    """FAB-6223 -> 6223 (mybill product id)."""
    return int(str(draft.fabric_code).split("-", 1)[1])


def goods_lines(draft: OrderDraft) -> list[dict[str, Any]]:
    """The cart as mybill expects it: item_code or product_id, quantity, size."""
    out = []
    for line in draft.lines or []:
        item: dict[str, Any] = {"quantity": line["quantity"]}
        if line.get("code"):
            item["item_code"] = line["code"]
        else:
            item["product_id"] = line["product_id"]
        if line.get("size"):
            item["size"] = line["size"]
        out.append(item)
    return out


def quote_request(draft: OrderDraft) -> dict[str, Any]:
    if flow.is_goods(draft):
        goods: dict[str, Any] = {"lines": goods_lines(draft)}
        if draft.pincode:
            goods["pincode"] = draft.pincode
        return goods
    if flow.is_fabric(draft):
        fabric: dict[str, Any] = {"product_id": fabric_product_id(draft), "metres": float(draft.fabric_metres)}
        if draft.pincode:
            fabric["pincode"] = draft.pincode
        return fabric
    body: dict[str, Any] = {"design_code": draft.design_code, "size_code": draft.size_code,
                            "option_ids": list(draft.option_ids or []), "qty": draft.qty or 1}
    if draft.pincode:
        body["pincode"] = draft.pincode
    return body


async def get_quote(mybill: MybillClient, draft: OrderDraft) -> dict[str, Any]:
    """Fresh mybill price for the draft (garment, fabric or goods cart)."""
    if flow.is_goods(draft):
        return await mybill.goods_quote(quote_request(draft))
    if flow.is_fabric(draft):
        return await mybill.fabric_quote(quote_request(draft))
    return await mybill.quote(quote_request(draft))


def measurements_payload(draft: OrderDraft) -> dict[str, Any]:
    """Measurements as mybill expects them (only filled fields)."""
    raw = {"size_code": draft.size_code, "height_cm": draft.height_cm, "fit_preference": draft.fit_preference,
           "bust_in": draft.bust_in, "waist_in": draft.waist_in, "hip_in": draft.hip_in,
           "notes": draft.measurement_notes}
    return {k: (float(v) if hasattr(v, "is_finite") else v) for k, v in raw.items() if v not in (None, "")}


def order_attribution(conversation: Conversation | None) -> dict[str, Any]:
    """source_type / source_id / ctwa_clid from the conversation's newest ad / post entry ({} if organic).
    mybill only accepts a numeric source_id, so anything else is left out."""
    if conversation is None:
        return {}
    source_type = mybill_source_type(conversation.entry_source_type)
    if not source_type or not (conversation.entry_source_id or "").isdigit():
        return {}
    out: dict[str, Any] = {"source_type": source_type, "source_id": conversation.entry_source_id}
    if conversation.entry_ctwa_clid:
        out["ctwa_clid"] = conversation.entry_ctwa_clid[:500]
    return out


def order_request(draft: OrderDraft, customer_id: int, channel: str | None = None,
                  conversation: Conversation | None = None) -> dict[str, Any]:
    """POST /api/agent/orders (or /orders/fabric) body. The idempotency key ties the order to this exact draft version."""
    body = {
        **order_attribution(conversation),
        **quote_request(draft),
        "customer_id": customer_id,
        "delivery": {"name": draft.delivery_name, "phone": draft.delivery_phone, "address_line1": draft.address_line1,
                     "address_line2": draft.address_line2, "city": draft.city, "state": draft.state,
                     "pincode": draft.pincode},
        "channel": channel or (conversation.channel.upper() if conversation is not None else "WHATSAPP"),
        "idempotency_key": f"{draft.id}:{draft.version}",
    }
    body.pop("pincode", None)   # the delivery address carries it
    if not flow.is_fabric(draft) and not flow.is_goods(draft):
        body["measurements"] = measurements_payload(draft)
    return body


def _fmt_date(value: str | None) -> str | None:
    try:
        return date.fromisoformat(value).strftime("%d %b %Y") if value else None
    except ValueError:
        return value


def _metres(value: Any) -> str:
    return f"{float(value):g} m"


def goods_line_text(line: dict[str, Any]) -> str:
    """'🧵 MCS-1B Blue check – 2.5 m × ₹300 = ₹750' / '👗 KUR-3A Kurti – size M × 2 × ₹899 = ₹1,798'."""
    label = " ".join(p for p in (line.get("item_code") or "", line.get("colour") or line.get("name") or "") if p)
    qty = float(line["quantity"])
    if line.get("kind") == "FABRIC":
        return f"🧵 {label} – {qty:g} m × {format_rupees(line['unit_price'])} = {format_rupees(line['line_total'])}"
    size = f"size {line['size']} " if line.get("size") else ""
    return f"👗 {label} – {size}× {qty:g} × {format_rupees(line['unit_price'])} = {format_rupees(line['line_total'])}"


def summary_text(draft: OrderDraft, design: dict[str, Any], quote: dict[str, Any], note: str | None = None) -> str:
    """The order summary - every price and date exactly as returned by the mybill quote.
    For a fabric draft `design` is the fabric (name, code, price_per_metre)."""
    b = quote["breakdown"]
    fit = (draft.fit_preference or "").title()
    size_line = ("📏 Size: Custom (our tailor will call you after payment)" if draft.size_code == flow.CUSTOM_SIZE
                 else f"📏 Size {draft.size_code}") + (f" · Height {float(draft.height_cm):g} cm" if draft.height_cm else "") \
        + (f" · {fit} fit" if fit else "")
    opts = ", ".join(f"{o['name']} ({'+' + format_rupees(o['price_delta']) if float(o['price_delta']) else 'included'})"
                     for o in b.get("options", []))
    address = ", ".join(p for p in (draft.delivery_name, draft.address_line1, draft.address_line2, draft.city,
                                    f"{draft.state} {draft.pincode}") if p)
    delivery = format_rupees(b["delivery_charge"]) if float(b["delivery_charge"]) else "Free"
    if flow.is_goods(draft):
        item_lines = [goods_line_text(line) for line in quote.get("lines", [])]
    elif flow.is_fabric(draft):
        item_lines = [f"🧵 {design.get('name', draft.fabric_code)} ({draft.fabric_code}) – fabric only",
                      f"📏 {_metres(draft.fabric_metres)} × {format_rupees(quote.get('price_per_metre', b['unit_price']))}/m"]
    else:
        item_lines = [f"👗 {design.get('name', draft.design_code)} ({draft.design_code})", size_line,
                      *([f"✂️ {opts}"] if opts else []), f"🔢 Qty {b.get('qty', draft.qty)}"]
    lines = ([note, ""] if note else []) + [
        "Please check your order 👇", "",
        *item_lines,
        "",
        f"📦 {address}",
        f"📞 {draft.delivery_phone}",
        "",
        f"Subtotal {format_rupees(b['subtotal'])} · Delivery {delivery}",
        f"*Total {format_rupees(b['total'])}*",
        *([f"🚚 Estimated delivery: {d}"] if (d := _fmt_date(quote.get("estimated_delivery_date"))) else []),
        "",
        "Tap ✅ Confirm to place the order, or ✏️ Change to edit.",
    ]
    return "\n".join(lines)


async def send_summary(conversation: Conversation, draft: OrderDraft, mybill: MybillClient, messenger: Messenger,
                       design: dict[str, Any], note: str | None = None) -> dict[str, Any]:
    """Fresh quote -> ONE summary message with Confirm/Change buttons; marks this version as awaiting confirmation."""
    quote = await get_quote(mybill, draft)
    if not quote.get("serviceable", True):
        return {"error": "not_serviceable", "pincode": draft.pincode}
    draft.last_quote = quote
    buttons = [{"id": f"confirm:{draft.id}:{draft.version}", "title": "✅ Confirm"},
               {"id": f"change:{draft.id}", "title": "✏️ Change"}]
    meta = {"kind": SUMMARY_KIND, "draft_id": str(draft.id), "version": draft.version}
    if not await messenger.send_buttons(summary_text(draft, design, quote, note), buttons, meta):
        return {"error": "send_failed"}
    draft.summary_sent_version = draft.version
    return {"sent": True, "total": quote["breakdown"]["total"],
            "estimated_delivery_date": quote.get("estimated_delivery_date"),
            "note": "The summary with Confirm/Change buttons is on her screen. Wait for her to confirm; "
                    "the system places the order and sends the confirmation, not you."}


# ── Payment links (mybill owns payments; we only ask for the link) ───────────

async def payment_link(mybill: MybillClient, order_no: str) -> dict[str, Any] | None:
    """mybill's active Razorpay link for the order (or a new one). None if it can't be made right now."""
    try:
        return await mybill.create_payment_link(order_no)
    except (MybillUnavailable, MybillRejected) as exc:
        logger.error("Payment link unavailable", extra={"fields": {"order_no": order_no, "error": str(exc)}})
        return None


def payment_text(order_no: str, link: dict[str, Any]) -> str:
    return (f"Pay securely for order {order_no} ({format_rupees(link['amount'])}) here 👉 {link['short_url']}\n"
            f"The link is valid for 24 hours.")


def confirmation_text(order_no: str, total: Any, link: dict[str, Any] | None) -> str:
    head = f"🎉 Order {order_no} created! Total {format_rupees(total)}."
    if link and link.get("short_url"):
        return f"{head}\n\n{payment_text(order_no, link)}"
    return f"{head} We'll send your payment link shortly."


# ── Confirmation (code path) ─────────────────────────────────────────────────

def is_affirmative(text: str | None) -> bool:
    cleaned = re.sub(r"[\s.!]+$", "", (text or "").strip().lower())
    return cleaned in AFFIRMATIVE


def parse_confirm_id(reply_id: str | None) -> tuple[uuid.UUID, int] | None:
    match = CONFIRM_RE.match(reply_id or "")
    return (uuid.UUID(match.group(1)), int(match.group(2))) if match else None


def is_summary(message: Message | None) -> bool:
    return bool(message and message.direction == "out" and (message.raw or {}).get("kind") == SUMMARY_KIND)


@dataclass
class ConfirmOutcome:
    """created | already_submitted | resent_summary | not_ready | unknown_draft | unavailable | rejected | send_failed"""

    kind: str
    order_no: str | None = None
    detail: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)


def find_confirmation(pending: list[Message], last_outbound: Message | None,
                      draft: OrderDraft | None) -> tuple[uuid.UUID, int] | None:
    """(draft_id, version) the customer confirmed, if any message in this batch is a confirmation."""
    for message in pending:
        parsed = parse_confirm_id(message.reply_id)
        if parsed:
            return parsed
        if is_affirmative(message.text) and is_summary(last_outbound):
            raw = last_outbound.raw or {}
            try:
                return uuid.UUID(raw["draft_id"]), int(raw["version"])
            except (KeyError, ValueError, TypeError):
                continue
    return None


async def confirm(conversation: Conversation, draft: OrderDraft | None, confirmed: tuple[uuid.UUID, int],
                  store: OrderStore, mybill: MybillClient, messenger: Messenger,
                  design_loader: Any, fabric_loader: Any = None) -> ConfirmOutcome:
    """Create the mybill order for a confirmed summary - at most once, and only for the version she saw."""
    draft_id, version = confirmed
    if draft is None or draft.id != draft_id:
        earlier = await store.get_draft(draft_id)
        if earlier is not None and earlier.status == DRAFT_SUBMITTED:
            return ConfirmOutcome("already_submitted", earlier.mybill_order_no)   # double tap / retry: stay silent
        return ConfirmOutcome("already_submitted" if earlier is not None else "unknown_draft")

    if draft.status != DRAFT_OPEN:
        return ConfirmOutcome("already_submitted", draft.mybill_order_no)

    fabric = flow.is_fabric(draft)
    goods = flow.is_goods(draft)
    design = {} if goods else await (fabric_loader(draft.fabric_code) if fabric else design_loader(draft.design_code))
    if version != draft.version or draft.summary_sent_version != draft.version:
        # She confirmed an older summary: show the current one instead of ordering something she didn't see -
        # but only if the order is complete again; otherwise let the agent continue collecting details.
        groups, by_group, _ = ({}, {}, {}) if (fabric or goods) else option_maps(design)
        incomplete = [f for s in (flow.SIZING, flow.CUSTOMISE, flow.ADDRESS)
                      for f in flow.missing_for(s, draft, groups, by_group)]
        if incomplete:
            return ConfirmOutcome("not_ready", detail=", ".join(incomplete))
        flow.set_stage(conversation, flow.CONFIRM)
        result = await send_summary(conversation, draft, mybill, messenger, design,
                                    note="Your order details changed, so here is the updated summary.")
        if result.get("error") == "send_failed":
            return ConfirmOutcome("send_failed")
        return ConfirmOutcome("resent_summary", detail=result.get("error"))

    customer_id = draft.mybill_customer_id or conversation.mybill_customer_id
    try:
        if customer_id is None:
            customer_id = (await mybill.upsert_customer(**customer_identity(conversation)))["customer_id"]
            conversation.mybill_customer_id = draft.mybill_customer_id = customer_id
        request = order_request(draft, customer_id, conversation=conversation)
        order = await (mybill.create_goods_order(request) if goods else
                       mybill.create_fabric_order(request) if fabric else mybill.create_order(request))
    except MybillUnavailable as exc:
        return ConfirmOutcome("unavailable", detail=str(exc))
    except MybillRejected as exc:
        logger.error("mybill rejected order", extra={"fields": {"draft_id": str(draft.id), "code": exc.code}})
        return ConfirmOutcome("rejected", detail=exc.code)

    previous = (draft.status, draft.mybill_order_no, conversation.stage)
    draft.status, draft.mybill_order_no = DRAFT_SUBMITTED, order["order_no"]
    flow.set_stage(conversation, flow.ORDER_CREATED)
    link = await payment_link(mybill, order["order_no"])
    text = confirmation_text(order["order_no"], order["total"], link)
    if not await messenger.send_text(text):
        # Not committed as submitted: the retry re-sends with the same idempotency key and gets the same order.
        draft.status, draft.mybill_order_no, conversation.stage = previous
        return ConfirmOutcome("send_failed", order["order_no"])
    logger.info("ORDER_CREATED", extra={"fields": {
        "conversation_id": str(conversation.id), "draft_id": str(draft.id), "version": draft.version,
        "order_no": order["order_no"], "total": order["total"]}})
    return ConfirmOutcome("created", order["order_no"], actions=[{"action": "order_created", "order_no": order["order_no"]}])

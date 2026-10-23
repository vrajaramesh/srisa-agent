"""The agent's tools: JSON schema for the model + an async handler each, gated by conversation stage.

- The runner offers only the tools allowed in the current stage (plus ALWAYS tools); each handler re-checks
  the stage too and returns {"error": "not_allowed_in_stage"} if called wrongly.
- Identity is never a tool input: the customer, her phone number, the mybill customer id and idempotency keys
  come from ToolContext. Prices and totals only ever come from mybill tool results.
- Stage changes happen only via app.agent.flow inside handlers.
- Handlers never raise for expected problems; they return {"error": ...} so the model can respond.
"""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agent import flow, ig_handoff, orders
from app.config import get_agent_settings, get_settings
from app.agent.flow import ADDRESS, BROWSING, CONFIRM, CUSTOMISE, ORDER_CREATED, SIZING
from app.agent.formatting import format_rupees
from app.models import DRAFT_CANCELLED, DRAFT_GARMENT, DRAFT_GOODS, DRAFT_OPEN, Conversation, OrderDraft
from app.mybill_client import MybillClient, MybillNotFound, MybillRejected, MybillUnavailable

logger = logging.getLogger(__name__)

MAX_PHOTOS = 3
MAX_LATEST = 6
MAX_FABRICS = 8
MAX_FABRIC_CARDS = 3
MAX_COLLECTION_PHOTOS = 10
METRE_UNITS = ("m", "meter", "meters", "metre", "metres", "mts", "mtr", "mtrs")
SHOP_NAME = "srisafabrics.com"
HANDOFF_MESSAGE = "A member of our team will reply to you here soon."
FITS = ("SNUG", "REGULAR", "RELAXED")
RANGES = {"height_cm": (120, 200), "bust_in": (24, 60), "waist_in": (20, 56), "hip_in": (26, 64)}
ALWAYS = frozenset({"*"})


@dataclass
class ToolContext:
    """Everything a tool may touch. conversation/draft are mutated in place; the worker's session persists them."""

    conversation: Conversation
    mybill: MybillClient | None
    messenger: orders.Messenger
    store: orders.OrderStore
    draft: OrderDraft | None = None  # the open draft, if any
    actions: list[dict[str, Any]] = field(default_factory=list)
    designs: dict[str, dict[str, Any]] = field(default_factory=dict)  # per-run cache
    fabrics: dict[str, dict[str, Any]] = field(default_factory=dict)  # per-run cache
    size_chart: list[dict[str, Any]] | None = None

    @property
    def stage(self) -> str:
        return flow.stage_of(self.conversation)

    async def design(self, code: str) -> dict[str, Any]:
        """mybill design (cached for this run). Raises MybillNotFound / MybillUnavailable."""
        if self.mybill is None:
            raise MybillUnavailable("mybill not configured")
        code = code.upper()
        if code not in self.designs:
            self.designs[code] = await self.mybill.get_design(code)
        return self.designs[code]

    async def fabric(self, code: str) -> dict[str, Any]:
        """mybill fabric by FAB-<id> code (cached for this run). Raises MybillNotFound / MybillUnavailable."""
        if self.mybill is None:
            raise MybillUnavailable("mybill not configured")
        code = code.upper()
        if code not in self.fabrics:
            self.fabrics[code] = await self.mybill.get_fabric(code)
        return self.fabrics[code]

    async def chart(self) -> list[dict[str, Any]]:
        if self.mybill is None:
            raise MybillUnavailable("mybill not configured")
        if self.size_chart is None:
            self.size_chart = await self.mybill.get_size_chart()
        return self.size_chart


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]
    stages: frozenset[str] = ALWAYS

    def allowed_in(self, stage: str) -> bool:
        return self.stages is ALWAYS or stage in self.stages

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


# ── helpers ──────────────────────────────────────────────────────────────────

def _code(value: Any) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _number(args: dict[str, Any], key: str) -> tuple[Decimal | None, dict[str, Any] | None]:
    """Optional number within RANGES[key] -> (value, None) or (None, error)."""
    if args.get(key) in (None, ""):
        return None, None
    try:
        value = Decimal(str(args[key]))
    except (InvalidOperation, ValueError):
        return None, {"error": "invalid_input", "field": key}
    low, high = RANGES[key]
    if not low <= value <= high:
        return None, {"error": "out_of_range", "field": key, "allowed": f"{low}-{high}"}
    return value.quantize(Decimal("0.1")), None


def _unavailable() -> dict[str, Any]:
    return {"error": "temporarily_unavailable"}


async def _fetch_design(ctx: ToolContext, code: str | None) -> dict[str, Any]:
    if not code:
        return {"error": "invalid_input", "detail": "code is required, e.g. KUR-101"}
    try:
        return await ctx.design(code)
    except MybillNotFound:
        return {"error": "not_found", "code": code}
    except MybillUnavailable:
        return _unavailable()


async def progress(ctx: ToolContext) -> dict[str, Any]:
    """Stage, filled draft fields, what's missing next, and whether a summary awaits confirmation."""
    draft, stage = ctx.draft, ctx.stage
    groups: set[str] = set()
    by_group: dict[int, str] = {}
    if draft and draft.design_code and not flow.is_fabric(draft):
        try:
            groups, by_group, _ = orders.option_maps(await ctx.design(draft.design_code))
        except (MybillNotFound, MybillUnavailable):
            pass
    result: dict[str, Any] = {
        "stage": stage,
        "draft": flow.draft_summary(draft),
        "missing_fields": flow.missing_fields(stage, draft, groups, by_group),
        "summary_awaiting_confirmation": flow.summary_awaiting_confirmation(draft),
    }
    if draft and groups:
        result["option_groups_to_choose"] = sorted(groups - flow.chosen_groups(draft, by_group))
    if stage == ORDER_CREATED:
        result["note"] = "Her order is created. For payment or status questions use get_order_status / resend_payment_link."
    return result


async def _after_sizing_change(ctx: ToolContext) -> dict[str, Any]:
    """Advance sizing -> customise when size+height+fit are present, and save the profile to mybill."""
    draft = ctx.draft
    assert draft is not None
    result: dict[str, Any] = {}
    if ctx.stage == SIZING and flow.next_stage(SIZING, draft) == CUSTOMISE:
        flow.set_stage(ctx.conversation, CUSTOMISE)
        if ctx.mybill is not None and draft.mybill_customer_id:
            try:
                await ctx.mybill.save_measurements(draft.mybill_customer_id, orders.measurements_payload(draft))
                result["measurements_saved"] = True
            except (MybillUnavailable, MybillRejected) as exc:
                logger.warning("Could not save measurements to mybill", extra={"fields": {"error": str(exc)}})
                result["measurements_saved"] = False
    return {**result, **(await progress(ctx))}


def _require_draft(ctx: ToolContext) -> dict[str, Any] | None:
    if ctx.draft is None or ctx.draft.status != DRAFT_OPEN:
        return {"error": "no_open_order", "hint": "Call start_order first."}
    return None


# ── always ───────────────────────────────────────────────────────────────────

async def get_design(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    design = await _fetch_design(ctx, _code(args.get("code")))
    if "error" in design:
        return design
    return {
        "code": design["code"], "name": design["name"], "base_price": design["base_price"],
        "currency": design.get("currency", "INR"), "description": design.get("description"),
        "fabric": design.get("fabric_description"), "lead_time_days": design.get("lead_time_days"),
        "option_groups": [
            {"group": g["group"], "options": [
                {"option_id": o["option_id"], "name": o["name"], "price_delta": o["price_delta"]} for o in g["options"]]}
            for g in design.get("option_groups", [])
        ],
        "image_count": len(design.get("images", [])),
        "exact_measurements_needed": bool(design.get("exact_measurements")),
        "fabric_only": [_link_brief(f) for f in design.get("fabrics") or [] if f.get("online") is not False],
    }


async def get_order_progress(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await progress(ctx)


async def cancel_order_draft(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.draft is None or ctx.draft.status != DRAFT_OPEN:
        return {"ok": True, "note": "There was no order in progress."}
    ctx.draft.status = DRAFT_CANCELLED
    flow.touch(ctx.draft)
    ctx.draft = None
    flow.set_stage(ctx.conversation, BROWSING)
    ctx.actions.append({"action": "order_draft_cancelled"})
    return {"ok": True, "stage": BROWSING}


async def get_order_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    order_no = await ctx.store.latest_order_no(ctx.conversation)
    if order_no is None:
        return {"error": "no_order", "note": "She has no confirmed order yet."}
    if ctx.mybill is None:
        return _unavailable()
    try:
        order = await ctx.mybill.get_order(order_no)
        payment = await ctx.mybill.get_payment(order_no)
    except (MybillUnavailable, MybillNotFound):
        return _unavailable()
    result: dict[str, Any] = {"order_no": order_no, "status": order["status"], "total": order["total"],
                              "estimated_delivery_date": order.get("estimated_delivery_date")}
    if payment:
        result["payment_status"] = payment["status"]
        if payment["status"] == "created":
            result["payment_link"] = payment.get("short_url")
    result["note"] = {
        "DRAFT": "Not paid yet - offer the payment link (resend_payment_link).",
        "PAYMENT_PENDING": "Waiting for payment - she can use the link, or ask for a new one (resend_payment_link).",
        "PAID": "Paid - the tailor is working on it.",
    }.get(order["status"], "Tell her the status in simple words.")
    return result


async def resend_payment_link(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    order_no = await ctx.store.latest_order_no(ctx.conversation)
    if order_no is None:
        return {"error": "no_order"}
    if ctx.mybill is None:
        return _unavailable()
    try:
        link = await ctx.mybill.create_payment_link(order_no)
    except MybillRejected as exc:
        if exc.code == "ORDER_NOT_PAYABLE":
            return {"error": "already_paid_or_cancelled", "order_no": order_no}
        return _unavailable()
    except (MybillUnavailable, MybillNotFound):
        return _unavailable()
    if not await ctx.messenger.send_text(orders.payment_text(order_no, link)):
        return {"error": "send_failed"}
    ctx.actions.append({"action": "payment_link_sent", "order_no": order_no})
    return {"sent": True, "order_no": order_no, "note": "The link is on her screen; don't repeat the URL."}


async def request_human_help(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason") or "unspecified").strip()[:200]
    ctx.conversation.needs_human = True
    ctx.conversation.needs_human_reason = reason
    ctx.actions.append({"action": "human_handoff", "reason": reason})
    logger.warning("HUMAN_HANDOFF", extra={"fields": {
        "conversation_id": str(ctx.conversation.id), "reason": reason, "customer": ctx.conversation.external_user_id}})
    return {"ok": True, "tell_customer": HANDOFF_MESSAGE,
            "note": "The team now handles this chat; you will not see her next messages."}


# ── browsing ─────────────────────────────────────────────────────────────────

async def list_latest_designs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.mybill is None:
        return _unavailable()
    try:
        designs = await ctx.mybill.list_latest_designs(_int(args.get("limit"), 4, 1, MAX_LATEST))
    except MybillUnavailable:
        return _unavailable()
    return {"designs": [{"code": d["code"], "name": d["name"], "base_price": d["base_price"],
                         "has_photo": bool(d.get("image_url"))} for d in designs]}


async def get_size_chart(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"unit": "inches (body measurements)", "sizes": await ctx.chart(),
                "custom_size": "If none fits, she can choose a custom size (size_code CUSTOM): our tailor calls her "
                               "after payment to take her exact measurements."}
    except MybillUnavailable:
        return _unavailable()


async def send_design_photos(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    design = await _fetch_design(ctx, _code(args.get("code")))
    if "error" in design:
        return design
    images = design.get("images", [])[: _int(args.get("max_photos"), MAX_PHOTOS, 1, MAX_PHOTOS)]
    if not images:
        return {"sent": 0, "code": design["code"], "note": "this design has no photos yet"}
    caption = f"{design['name']} – {format_rupees(design.get('base_price'))}"
    sent = 0
    for i, image in enumerate(images):
        if await ctx.messenger.send_image(image["url"], caption if i == 0 else None):
            sent += 1
    ctx.actions.append({"action": "sent_photos", "code": design["code"], "count": sent})
    result: dict[str, Any] = {"sent": sent, "code": design["code"]}
    if sent < len(images):
        result["error"] = "some_photos_failed"
    return result


async def set_current_design(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    design = await _fetch_design(ctx, _code(args.get("code")))
    if "error" in design:
        return design
    ctx.conversation.current_product_code = design["code"]
    ctx.actions.append({"action": "set_current_design", "code": design["code"]})
    return {"ok": True, "code": design["code"], "name": design["name"]}


async def start_order(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    design = await _fetch_design(ctx, _code(args.get("design_code")))
    if "error" in design:
        return design
    if ctx.mybill is None:
        return _unavailable()
    try:
        customer = await ctx.mybill.upsert_customer(**orders.customer_identity(ctx.conversation))
        saved = await ctx.mybill.get_latest_measurements(customer["customer_id"])
    except (MybillUnavailable, MybillRejected):
        return _unavailable()

    draft = ctx.draft if ctx.draft is not None and ctx.draft.status == DRAFT_OPEN else None
    if draft is None:
        draft = await ctx.store.new_draft(ctx.conversation)
        ctx.draft = draft
    elif draft.design_code != design["code"]:
        draft.option_ids = []  # options belong to the old design
    draft.kind, draft.fabric_code, draft.fabric_metres = DRAFT_GARMENT, None, None   # fabric drafts are legacy
    draft.design_code = design["code"]
    draft.mybill_customer_id = customer["customer_id"]
    ctx.conversation.mybill_customer_id = customer["customer_id"]
    ctx.conversation.current_product_code = design["code"]
    flow.touch(draft)
    flow.set_stage(ctx.conversation, SIZING if flow.missing_for(SIZING, draft) else CUSTOMISE)
    ctx.actions.append({"action": "order_started", "code": design["code"]})

    result: dict[str, Any] = {"ok": True, "design": {"code": design["code"], "name": design["name"]},
                              "has_saved_measurements": saved is not None}
    if design.get("exact_measurements"):
        result["exact_measurements_needed"] = True
        result["tell_customer"] = ("For this design our tailor will call her after payment to take her exact "
                                   "measurements; she still picks her nearest size now.")
    if saved:
        result["saved_measurements"] = {k: saved.get(k) for k in
                                        ("size_code", "height_cm", "fit_preference", "bust_in", "waist_in", "hip_in")
                                        if saved.get(k) is not None}
        result["hint"] = "Ask if she wants to reuse her saved measurements (use_saved_measurements)."
    return {**result, **(await progress(ctx))}


# ── sizing ───────────────────────────────────────────────────────────────────

async def send_size_options(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        chart = await ctx.chart()
    except MybillUnavailable:
        return _unavailable()
    rows = [{"id": f"size:{s['size_code']}", "title": s["size_code"],
             "description": f"Bust {s['bust_in']:g} · Waist {s['waist_in']:g} · Hip {s['hip_in']:g} in"}
            for s in chart]
    rows.append({"id": f"size:{flow.CUSTOM_SIZE}", "title": "Custom size",
                 "description": "Our tailor calls you after payment to take your measurements"})
    ok = await ctx.messenger.send_list("Please choose your size 👇 (body measurements in inches)", "Choose size",
                                       [{"title": "Sizes", "rows": rows}])
    return {"sent": ok, "sizes": [s["size_code"] for s in chart] + [flow.CUSTOM_SIZE]} if ok else {"error": "send_failed"}


async def send_fit_options(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ok = await ctx.messenger.send_buttons("How would you like it to fit?", [
        {"id": "fit:SNUG", "title": "Snug fit"}, {"id": "fit:REGULAR", "title": "Regular fit"},
        {"id": "fit:RELAXED", "title": "Relaxed fit"}])
    return {"sent": True} if ok else {"error": "send_failed"}


async def save_size(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    draft = ctx.draft
    updates: dict[str, Any] = {}
    if args.get("size_code") not in (None, ""):
        code = str(args["size_code"]).strip().upper()
        if code in ("CUSTOM SIZE", "CUSTOM_SIZE"):
            code = flow.CUSTOM_SIZE
        try:
            valid = [s["size_code"] for s in await ctx.chart()] + [flow.CUSTOM_SIZE]
        except MybillUnavailable:
            return _unavailable()
        if code not in valid:
            return {"error": "invalid_size", "valid_sizes": valid,
                    "hint": "Sizes outside our chart need the team: request_human_help."}
        updates["size_code"] = code
    height, err = _number(args, "height_cm")
    if err:
        return err
    if height is not None:
        updates["height_cm"] = height
    if args.get("fit_preference") not in (None, ""):
        fit = str(args["fit_preference"]).strip().upper()
        if fit not in FITS:
            return {"error": "invalid_input", "field": "fit_preference", "allowed": list(FITS)}
        updates["fit_preference"] = fit
    if not updates:
        return {"error": "invalid_input", "detail": "give size_code, height_cm and/or fit_preference"}
    for key, value in updates.items():
        setattr(draft, key, value)
    flow.touch(draft)
    return await _after_sizing_change(ctx)


async def save_custom_measurements(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    draft = ctx.draft
    changed = False
    for key in ("bust_in", "waist_in", "hip_in"):
        value, err = _number(args, key)
        if err:
            return err
        if value is not None:
            setattr(draft, key, value)
            changed = True
    if args.get("notes"):
        draft.measurement_notes = str(args["notes"]).strip()[:500]
        changed = True
    if not changed:
        return {"error": "invalid_input", "detail": "give bust_in, waist_in, hip_in and/or notes"}
    flow.touch(draft)
    return await _after_sizing_change(ctx)


async def use_saved_measurements(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    draft = ctx.draft
    if ctx.mybill is None or not draft.mybill_customer_id:
        return _unavailable()
    try:
        saved = await ctx.mybill.get_latest_measurements(draft.mybill_customer_id)
    except MybillUnavailable:
        return _unavailable()
    if not saved:
        return {"error": "no_saved_measurements"}
    for key in ("size_code", "height_cm", "fit_preference", "bust_in", "waist_in", "hip_in"):
        if saved.get(key) is not None:
            setattr(draft, key, saved[key])
    if saved.get("notes"):
        draft.measurement_notes = saved["notes"]
    flow.touch(draft)
    return await _after_sizing_change(ctx)


# ── customise ────────────────────────────────────────────────────────────────

def _delta_label(delta: Any) -> str:
    return f"+{format_rupees(delta)}" if float(delta) else "included"


async def send_customisation_options(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    design = await _fetch_design(ctx, ctx.draft.design_code)
    if "error" in design:
        return design
    group = str(args.get("group") or "").strip().upper()
    match = next((g for g in design.get("option_groups", []) if g["group"] == group), None)
    if match is None:
        return {"error": "unknown_group", "groups": [g["group"] for g in design.get("option_groups", [])]}
    options = match["options"]
    body = f"Choose {group.lower().replace('_', ' ')} 👇"
    if len(options) <= 3:
        ok = await ctx.messenger.send_buttons(body, [
            {"id": f"opt:{o['option_id']}",
             "title": o["name"] if not float(o["price_delta"]) else f"{o['name']} +{format_rupees(o['price_delta'])}"}
            for o in options])
    else:
        ok = await ctx.messenger.send_list(body, "Choose", [{"title": group.title(), "rows": [
            {"id": f"opt:{o['option_id']}", "title": o["name"], "description": _delta_label(o["price_delta"])}
            for o in options]}])
    return {"sent": True, "group": group, "count": len(options)} if ok else {"error": "send_failed"}


async def choose_option(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    draft = ctx.draft
    try:
        option_id = int(args.get("option_id"))
    except (TypeError, ValueError):
        return {"error": "invalid_input", "field": "option_id"}
    design = await _fetch_design(ctx, draft.design_code)
    if "error" in design:
        return design
    _, by_group, options = orders.option_maps(design)
    if option_id not in options:  # other design's option, inactive, or unknown
        return {"error": "invalid_option", "option_id": option_id}
    group = by_group[option_id]
    replaced = [i for i in (draft.option_ids or []) if by_group.get(i) == group and i != option_id]
    draft.option_ids = [i for i in (draft.option_ids or []) if by_group.get(i) != group] + [option_id]
    flow.touch(draft)
    result = {"ok": True, "chosen": {"group": group, "name": options[option_id]["name"],
                                     "price_delta": options[option_id]["price_delta"]}}
    if replaced:
        result["replaced_previous_choice"] = True
    return {**result, **(await progress(ctx))}


async def set_quantity(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    try:
        qty = int(args.get("qty"))
    except (TypeError, ValueError):
        return {"error": "invalid_input", "field": "qty"}
    if not 1 <= qty <= flow.MAX_QTY:
        return {"error": "out_of_range", "field": "qty", "allowed": f"1-{flow.MAX_QTY}",
                "hint": "More than 5 pieces is a bulk order: request_human_help."}
    ctx.draft.qty = qty
    flow.touch(ctx.draft)
    return {"ok": True, "qty": qty}


async def finish_customisation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    design = await _fetch_design(ctx, ctx.draft.design_code)
    if "error" in design:
        return design
    groups, by_group, _ = orders.option_maps(design)
    target = flow.next_stage(CUSTOMISE, ctx.draft, groups, by_group, finished_customisation=True)
    if target != ADDRESS:
        return {"error": "incomplete", "missing_fields": flow.missing_for(CUSTOMISE, ctx.draft, groups, by_group)}
    flow.set_stage(ctx.conversation, ADDRESS)
    return await progress(ctx)


# ── address ──────────────────────────────────────────────────────────────────

async def save_delivery_details(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    draft = ctx.draft
    fields = {"name": "delivery_name", "line1": "address_line1", "city": "city", "state": "state"}
    values = {col: str(args.get(key) or "").strip() for key, col in fields.items()}
    missing = [key for key, col in fields.items() if not values[col]]
    pincode = str(args.get("pincode") or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{5}", pincode):
        missing.append("pincode (6 digits, not starting with 0)")
    phone = str(args.get("phone") or "").strip() or orders.phone_e164(ctx.conversation)
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not 10 <= len(digits) <= 13:
        missing.append("phone (10-13 digits)")
    if missing:
        return {"error": "invalid_input", "missing_or_invalid": missing}
    for col, value in values.items():
        setattr(draft, col, value[:200])
    draft.address_line2 = (str(args.get("line2") or "").strip() or None)
    draft.pincode, draft.delivery_phone = pincode, phone
    flow.touch(draft)
    if ctx.mybill is None:
        return _unavailable()
    try:
        quote = await orders.get_quote(ctx.mybill, draft)
    except MybillUnavailable:
        return _unavailable()
    except MybillRejected as exc:
        return {"error": "quote_rejected", "code": exc.code, "message": exc.message}
    if not quote.get("serviceable", True):
        return {"error": "not_serviceable", "pincode": pincode,
                "hint": "Tell her we can't deliver there yet and offer the team's help (request_human_help)."}
    draft.last_quote = quote
    flow.set_stage(ctx.conversation, CONFIRM)
    return {"ok": True, "stage": CONFIRM, "next": "Call send_order_summary now."}


# ── confirm ──────────────────────────────────────────────────────────────────

async def send_order_summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if (err := _require_draft(ctx)) is not None:
        return err
    design = ({} if flow.is_goods(ctx.draft) else
              await _fetch_fabric(ctx, ctx.draft.fabric_code) if flow.is_fabric(ctx.draft)
              else await _fetch_design(ctx, ctx.draft.design_code))
    if "error" in design:
        return design
    try:
        return await orders.send_summary(ctx.conversation, ctx.draft, ctx.mybill, ctx.messenger, design)
    except MybillUnavailable:
        return _unavailable()
    except MybillRejected as exc:
        return {"error": "quote_rejected", "code": exc.code, "message": exc.message}


# ── fabric only: bought on the shop website (srisafabrics.com) ───────────────

def _fabric_brief(fabric: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the model may say about a fabric from search/get. Stock is only in stock or not (never a count)."""
    if not fabric:
        return None
    return {"code": fabric["code"], "name": fabric["name"], "price_per_metre": fabric["price_per_metre"],
            "category": fabric.get("category"), "description": fabric.get("description"),
            "in_stock": float(fabric.get("available_metres") or 0) > 0, "photo_count": len(fabric.get("images") or [])}


def _link_brief(link: dict[str, Any]) -> dict[str, Any]:
    """A design's fabric link (send it with send_fabric_links)."""
    return {"name": link.get("label") or link.get("name") or "Fabric", "price": link.get("price"),
            "unit": link.get("unit"), "in_stock": link.get("in_stock"), "product_id": link.get("product_id")}


async def _fetch_fabric(ctx: ToolContext, code: str | None) -> dict[str, Any]:
    code = (code or "").strip().upper()
    if not re.fullmatch(r"FAB-?\d{1,9}", code):
        return {"error": "invalid_input", "detail": "fabric code looks like FAB-6223"}
    try:
        return await ctx.fabric(code if "-" in code else f"FAB-{code[3:]}")
    except MybillNotFound:
        return {"error": "not_found", "code": code}
    except MybillUnavailable:
        return _unavailable()


def _per_unit(price: Any, unit: str | None) -> str:
    if price is None:
        return ""
    u = (unit or "").strip().lower()
    per = "m" if u in ("m", "meter", "meters", "metre", "metres", "mts", "mtr", "mtrs") else ("piece" if u.startswith("piece") else u)
    return f" – {format_rupees(price)}" + (f"/{per}" if per else "")


async def search_fabrics(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.mybill is None:
        return _unavailable()
    query = str(args.get("query") or "").strip()[:100]
    try:
        found = await ctx.mybill.search_fabrics(query, _int(args.get("limit"), 5, 1, MAX_FABRICS))
    except MybillUnavailable:
        return _unavailable()
    for f in found:
        ctx.fabrics[f["code"].upper()] = f
    if not found:
        return {"fabrics": [], "note": "Nothing in stock matches. Ask her to describe it differently or offer the team's help."}
    return {"fabrics": [_fabric_brief(f) for f in found]}


async def get_fabric(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    fabric = await _fetch_fabric(ctx, args.get("code"))
    return fabric if "error" in fabric else _fabric_brief(fabric)


async def send_fabric_photos(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    fabric = await _fetch_fabric(ctx, args.get("code"))
    if "error" in fabric:
        return fabric
    images = (fabric.get("images") or [])[:_int(args.get("max_photos"), MAX_PHOTOS, 1, MAX_PHOTOS)]
    if not images:
        return {"error": "no_photos", "note": "This fabric has no photos yet; send its website link instead (send_fabric_links)."}
    caption = f"{fabric['name']} ({fabric['code']}){_per_unit(fabric['price_per_metre'], 'm')}"
    for i, image in enumerate(images):
        if not await ctx.messenger.send_image(image["url"], caption if i == 0 else None):
            return {"error": "send_failed"}
    ctx.actions.append({"action": "fabric_photos_sent", "code": fabric["code"], "count": len(images)})
    return {"sent": len(images), "note": "The photos are on her screen."}


def design_fabrics(design: dict[str, Any]) -> list[dict[str, Any]]:
    """A design's fabrics that can be bought on the website now (online, in stock)."""
    return [{"name": f.get("label") or f.get("name") or "Fabric", "price": f.get("price"), "unit": f.get("unit"),
             "url": f["url"], "image": f.get("image_url")}
            for f in design.get("fabrics") or [] if f.get("online") is not False and f.get("in_stock") is not False]


def all_fabrics_url(design_code: str) -> str:
    """The website page listing every fabric of the design."""
    return f"{get_settings().shop_url.rstrip('/')}/?fabrics={design_code}"


def fabric_price_text(items: list[dict[str, Any]]) -> str:
    prices = sorted({float(i["price"]) for i in items if i.get("price") is not None})
    if not prices:
        return ""
    per = _per_unit(prices[0], items[0].get("unit")).split("/", 1)
    unit = f"/{per[1]}" if len(per) > 1 else ""
    return (f"{format_rupees(prices[0])}{unit}" if len(prices) == 1 else f"from {format_rupees(prices[0])}{unit}")


async def offer_fabric(ctx: ToolContext, design: dict[str, Any]) -> bool:
    """One short message after the design's first reply: the fabric is also sold alone, with a button to the
    website (the product itself, or the page listing all its fabrics). True if nothing to send or sent."""
    items = design_fabrics(design)
    if not items:
        return True
    url = items[0]["url"] if len(items) == 1 else all_fabrics_url(design["code"])
    price = fabric_price_text(items)
    body = ("🧵 Prefer only the fabric? We sell it separately too" + (f" – {price}" if price else "") +
            ". Buy it on our website 👇")
    sent = await ctx.messenger.send_link_button(body, "Shop the fabric", url)
    if sent:
        ctx.actions.append({"action": "fabric_offered", "code": design["code"], "options": len(items)})
    return sent


async def send_fabric_links(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Up to 3 fabrics as messages with photo, name, price and a "Buy on <shop>" button to its website page;
    for a design with more, one more button opens the website page listing all of them."""
    items: list[dict[str, Any]] = []
    design_code: str | None = None
    if args.get("design_code"):
        design = await _fetch_design(ctx, _code(args.get("design_code")))
        if "error" in design:
            return design
        design_code = design["code"]
        items = design_fabrics(design)
        if not items:
            return {"error": "no_fabric_links", "note": "This design's fabric isn't sold separately online. Offer "
                    "search_fabrics for similar fabrics or the team's help."}
    elif args.get("fabric_code"):
        fabric = await _fetch_fabric(ctx, args.get("fabric_code"))
        if "error" in fabric:
            return fabric
        if not fabric.get("shop_url"):
            return {"error": "no_link"}
        images = fabric.get("images") or []
        items = [{"name": fabric["name"], "price": fabric["price_per_metre"], "unit": "m", "url": fabric["shop_url"],
                  "image": images[0]["url"] if images else None}]
    else:
        return {"error": "invalid_input", "detail": "give design_code or fabric_code"}
    button = f"Buy on {SHOP_NAME}"
    for item in items[:MAX_FABRIC_CARDS]:
        body = f"🧵 {item['name']}{_per_unit(item['price'], item['unit'])}\nTap below to see it and buy on our website."
        if not await ctx.messenger.send_link_button(body, button, item["url"], item["image"]):
            return {"error": "send_failed"}
    if design_code and len(items) > MAX_FABRIC_CARDS:
        body = f"🧵 See all {len(items)} fabric options for this design on our website 👇"
        if not await ctx.messenger.send_link_button(body, f"See all {len(items)} fabrics", all_fabrics_url(design_code)):
            return {"error": "send_failed"}
    ctx.actions.append({"action": "fabric_links_sent", "count": len(items)})
    return {"sent": len(items),
            "note": "The links are on her screen. She buys on the website (add to cart, checkout there). Don't take "
                    "a fabric order in chat or repeat the URLs."}


# ── collections & goods cart (fabric metres / readymade sizes) ─────────────────

def _collection_brief(c: dict[str, Any]) -> dict[str, Any]:
    """What the model may say about a collection. Stock is only 'in stock' or the sizes in stock (no counts)."""
    items = []
    for i in c.get("items") or []:
        item = {"code": i["item_code"], "colour": i.get("colour"), "name": i.get("product_name"),
                "price": i.get("price"), "unit": "m" if (i.get("unit") or "").lower() in METRE_UNITS else "piece",
                "in_stock": bool(i.get("available")) and float(i.get("stock") or 0) > 0,
                "has_photo": bool(i.get("image_url"))}
        if i.get("sizes"):
            item["sizes_in_stock"] = [s["size"] for s in i["sizes"] if float(s["stock"]) > 0]
        if i.get("ai_instructions"):
            item["instructions"] = i["ai_instructions"]
        if item["unit"] == "m":
            item["max_metres"] = float(i.get("stock") or 0)
        items.append(item)
    return {"code": c["code"], "name": c["name"], "type": c["type"], "description": c.get("description"),
            "instructions": c.get("ai_instructions"), "items": items}


async def _load_collection(ctx: ToolContext, code: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(collection, the item code she named if `code` was an item) - by collection code or item code."""
    code = (code or "").strip().upper()
    if not code or ctx.mybill is None:
        return None, None
    try:
        return await ctx.mybill.get_collection(code), None
    except MybillNotFound:
        pass
    try:
        item = await ctx.mybill.get_collection_item(code)
        return await ctx.mybill.get_collection(item["collection"]["code"]), item["item_code"]
    except MybillNotFound:
        return None, None


async def get_collection(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.mybill is None:
        return _unavailable()
    try:
        collection, item_code = await _load_collection(ctx, args.get("code"))
    except MybillUnavailable:
        return _unavailable()
    if collection is None:
        return {"error": "not_found", "code": args.get("code")}
    if not collection.get("active", True):
        return {"error": "not_available", "note": "This collection isn't sold any more; offer other designs or the team."}
    ctx.conversation.current_collection_code = collection["code"]
    result = _collection_brief(collection)
    if item_code:
        result["she_named_item"] = item_code
    return result


async def send_collection_photos(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.mybill is None:
        return _unavailable()
    try:
        collection, _ = await _load_collection(ctx, args.get("code"))
    except MybillUnavailable:
        return _unavailable()
    if collection is None:
        return {"error": "not_found", "code": args.get("code")}
    wanted = {str(c).strip().upper() for c in (args.get("item_codes") or []) if str(c).strip()}
    items = [i for i in collection.get("items") or [] if i.get("image_url") and (not wanted or i["item_code"].upper() in wanted)]
    sent = 0
    for i in items[:MAX_COLLECTION_PHOTOS]:
        unit = "m" if (i.get("unit") or "").lower() in METRE_UNITS else "piece"
        caption = " · ".join(p for p in (i["item_code"], i.get("colour") or i.get("product_name"),
                                          f"{format_rupees(i.get('price'))}/{unit}" if i.get("price") is not None else "") if p)
        if not await ctx.messenger.send_image(i["image_url"], caption):
            return {"error": "send_failed"}
        sent += 1
    if not sent:
        return {"error": "no_photos"}
    ctx.conversation.current_collection_code = collection["code"]
    ctx.actions.append({"action": "collection_photos_sent", "code": collection["code"], "count": sent})
    return {"sent": sent, "note": "The photos (each captioned with its code) are on her screen. Ask which code(s) she likes."}


def _cart_key(line: dict[str, Any]) -> tuple[str, str]:
    ident = (line.get("code") or f"#{line.get('product_id')}").upper()
    return ident, (line.get("size") or "").strip().upper()


async def _requote_cart(ctx: ToolContext, lines: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """mybill prices and checks the whole cart (stock, sizes, metres). (quote, None) or (None, error result)."""
    if not lines:
        return {"lines": [], "breakdown": {"subtotal": 0}}, None
    request = [{"quantity": l["quantity"], **({"item_code": l["code"]} if l.get("code") else {"product_id": l["product_id"]}),
                **({"size": l["size"]} if l.get("size") else {})} for l in lines]
    try:
        return await ctx.mybill.goods_quote({"lines": request}), None
    except MybillUnavailable:
        return None, _unavailable()
    except MybillRejected as exc:
        return None, {"error": (exc.code or "rejected").lower(), "message": exc.message, **(exc.details or {}),
                      "hint": "Tell her simply and offer what is possible (another size, fewer metres/pieces)."}


def _cart_view(quote: dict[str, Any]) -> dict[str, Any]:
    return {"cart": [orders.goods_line_text(l) for l in quote.get("lines", [])],
            "subtotal": (quote.get("breakdown") or {}).get("subtotal")}


async def set_cart_item(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Add an item, or change its quantity (0 removes it). One line per item code (+ size for readymade)."""
    if ctx.mybill is None:
        return _unavailable()
    code = str(args.get("code") or "").strip().upper() or None
    product_id = args.get("product_id")
    if code and re.fullmatch(r"FAB-?\d{1,9}", code):          # a fabric from search_fabrics: by product id
        product_id, code = int(re.sub(r"\D", "", code)), None
    if not code and not isinstance(product_id, int):
        return {"error": "invalid_input", "detail": "give the item code (e.g. MCS-1B) or product_id"}
    try:
        quantity = Decimal(str(args.get("quantity")))
    except (InvalidOperation, ValueError):
        return {"error": "invalid_input", "detail": "quantity must be a number (metres for fabric, pieces for readymade)"}
    size = str(args.get("size") or "").strip() or None

    draft = ctx.draft if ctx.draft is not None and ctx.draft.status == DRAFT_OPEN else None
    if draft is not None and not flow.is_goods(draft):
        if ctx.stage != BROWSING and (draft.design_code or draft.fabric_code):
            return {"error": "stitched_order_in_progress",
                    "hint": "Ask her to finish or cancel the stitched order first (cancel_order_draft), or place it, then add these."}
        draft.kind, draft.design_code, draft.fabric_code, draft.fabric_metres, draft.option_ids = DRAFT_GOODS, None, None, None, []
        draft.lines = []
    new = {"code": code, "product_id": product_id, "quantity": float(quantity), "size": size}
    lines = [l for l in (draft.lines if draft else []) if _cart_key(l) != _cart_key(new)]
    if quantity > 0:
        lines.append(new)
    quote, error = await _requote_cart(ctx, lines)
    if error:
        return error
    if draft is None:
        try:
            customer = await ctx.mybill.upsert_customer(**orders.customer_identity(ctx.conversation))
        except (MybillUnavailable, MybillRejected):
            return _unavailable()
        draft = await ctx.store.new_draft(ctx.conversation)
        draft.kind, draft.mybill_customer_id = DRAFT_GOODS, customer["customer_id"]
        ctx.conversation.mybill_customer_id = customer["customer_id"]
        ctx.draft = draft
    draft.lines = [{"code": q.get("item_code"), "product_id": q["product_id"], "quantity": float(q["quantity"]),
                    "size": q.get("size"), "name": q.get("name"), "colour": q.get("colour"), "kind": q.get("kind"),
                    "unit": q.get("unit"), "unit_price": q.get("unit_price"), "line_total": q.get("line_total")}
                   for q in quote["lines"]]
    flow.touch(draft)
    if ctx.stage in (BROWSING, ORDER_CREATED, ADDRESS, CONFIRM):
        flow.set_stage(ctx.conversation, CUSTOMISE)    # the cart; finish_cart moves on (keeps a saved address)
    ctx.actions.append({"action": "cart_updated", "items": len(draft.lines)})
    return {"ok": True, **_cart_view(quote),
            "next": "Ask if she wants anything else (another colour/size). When she's done, call finish_cart."
                    if draft.lines else "The cart is empty."}


async def remove_cart_item(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await set_cart_item(ctx, {**args, "quantity": 0})


async def finish_cart(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Cart done: ask for the address - or, if it is already saved, re-price and go to the summary."""
    draft = ctx.draft
    if draft is None or draft.status != DRAFT_OPEN or not flow.is_goods(draft) or not draft.lines:
        return {"error": "empty_cart", "hint": "Add items with set_cart_item first."}
    flow.touch(draft)
    if flow.missing_for(ADDRESS, draft):
        flow.set_stage(ctx.conversation, ADDRESS)
        return {"ok": True, "stage": ADDRESS, "next": "Ask for her delivery name and address (save_delivery_details)."}
    try:
        draft.last_quote = await orders.get_quote(ctx.mybill, draft)
    except MybillUnavailable:
        return _unavailable()
    except MybillRejected as exc:
        return {"error": (exc.code or "rejected").lower(), "message": exc.message}
    flow.set_stage(ctx.conversation, CONFIRM)
    return {"ok": True, "stage": CONFIRM, "next": "Call send_order_summary now."}


def _go_back(target: str) -> Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        if (err := _require_draft(ctx)) is not None:
            return err
        try:
            flow.go_back(ctx.conversation, ctx.draft, target)
        except flow.FlowError as exc:
            return {"error": "not_allowed", "detail": str(exc)}
        return await progress(ctx)

    return handler


async def send_whatsapp_order_link(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Instagram only: orders are placed on WhatsApp. Sends an "Order on WhatsApp" button (wa.me link with a
    pre-typed message carrying the design and a ref code that links her Instagram account to her number)."""
    code = (_code(args.get("design_code")) or ctx.conversation.current_product_code
            or ctx.conversation.current_collection_code)
    design_code = None
    if code:
        design = await _fetch_design(ctx, code)
        if "error" not in design:
            design_code = design["code"]
            ctx.conversation.current_product_code = design_code
        else:   # a collection or item code (e.g. MCS-1, MCS-1B)
            try:
                collection, item_code = await _load_collection(ctx, code)
            except MybillUnavailable:
                return _unavailable()
            if collection is None:
                return design
            design_code = item_code or collection["code"]
            ctx.conversation.current_collection_code = collection["code"]
    ref = await ctx.store.create_ref_code(ctx.conversation, design_code)
    settings = get_agent_settings()
    # Short link on our domain (srisafabrics.com/wa/KUR-101/IG-XXXXX -> wa.me with the pre-typed message).
    # Sent as an "Order on WhatsApp" button (clickable in the Instagram app); the text carries the link too for
    # instagram.com in a browser, which doesn't show template buttons.
    url = (ig_handoff.short_link(settings.order_link_base, design_code, ref) if settings.order_link_base
           else ig_handoff.whatsapp_link(settings.whatsapp_order_number, ig_handoff.prefilled_text(design_code, ref)))
    what = f" {design_code}" if design_code else ""
    body = (f"To order{what}, continue on WhatsApp 👇 Just press Send there and I'll help you with size, metres "
            f"and payment.\n{url}")
    if not await ctx.messenger.send_link_button(body, "Order on WhatsApp", url):
        return {"error": "send_failed"}
    ctx.actions.append({"action": "whatsapp_order_link", "code": design_code, "ref": ref})
    return {"sent": True, "design_code": design_code, "ref": ref,
            "note": "The link opens WhatsApp with her order message ready; she only needs to press Send."}


# ── registry ─────────────────────────────────────────────────────────────────

_CODE = {"type": "string", "description": "Design code exactly as shown to the customer, e.g. KUR-101."}
_NUM = {"type": "number"}
_FAB = {"type": "string", "description": "Fabric code exactly as returned by search_fabrics/get_design, e.g. FAB-6223."}
SHOPPING = frozenset({BROWSING, ORDER_CREATED})

TOOLS: list[Tool] = [
    # Always available
    Tool("get_design",
         "Look up one of our designs by its code (like KUR-101): name, base price, fabric, stitching/lead time, "
         "customisation options (with option_id and extra charge) and photo count. Use it before saying anything "
         "about a design's price, fabric, options or timing. Returns {\"error\": \"not_found\"} if unknown.",
         {"type": "object", "properties": {"code": _CODE}, "required": ["code"]}, get_design),
    Tool("get_order_progress",
         "Where the order stands: stage, what is already filled in, what is still missing, and whether a summary "
         "is waiting for her confirmation. Call it whenever you are unsure what to ask next.",
         {"type": "object", "properties": {}}, get_order_progress),
    Tool("cancel_order_draft",
         "Cancel the order in progress when she clearly says she doesn't want to order any more.",
         {"type": "object", "properties": {}}, cancel_order_draft),
    Tool("get_order_status",
         "Her latest confirmed order: status (DRAFT, PAYMENT_PENDING, PAID, IN_STITCHING, DISPATCHED, DELIVERED), "
         "total, payment status and estimated delivery date. Use it for any question about an existing order or payment.",
         {"type": "object", "properties": {}}, get_order_status),
    Tool("resend_payment_link",
         "Send her the payment link for her latest order (a fresh one if the old link expired). Use it when she "
         "wants to pay, says the link expired, or asks for the link again.",
         {"type": "object", "properties": {}}, resend_payment_link, frozenset({ORDER_CREATED})),
    Tool("request_human_help",
         "Hand this chat to our shop team: she asks for a person, is upset/complaining, asks about returns, damaged "
         "items, bulk/wholesale (more than 5 pieces), custom designs, sizes outside our chart, when a tool reports "
         "temporarily_unavailable, or whenever you don't know. Then tell her warmly a team member will reply soon.",
         {"type": "object", "properties": {"reason": {"type": "string",
          "description": "Short reason, e.g. wants_person, complaint, size_outside_chart, temporarily_unavailable."}},
          "required": ["reason"]}, request_human_help),
    # Browsing (also after an order is created)
    Tool("list_latest_designs",
         "Our newest designs (code, name, base price). Use it when she wants to see what's new or hasn't said "
         "which design she likes, then ask which one interests her.",
         {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LATEST}}},
         list_latest_designs, SHOPPING),
    Tool("send_design_photos",
         "Send her up to 3 photos of a design, captioned with name and price. After calling it just say the photos "
         "are on the way.",
         {"type": "object", "properties": {"code": _CODE, "max_photos": {"type": "integer", "minimum": 1,
          "maximum": MAX_PHOTOS}}, "required": ["code"]}, send_design_photos, SHOPPING),
    Tool("set_current_design",
         "Remember which design she is currently interested in (checked against our catalog).",
         {"type": "object", "properties": {"code": _CODE}, "required": ["code"]}, set_current_design, SHOPPING),
    Tool("get_size_chart",
         "Our size chart (XS to 4XL) with body bust, waist, hip, shoulder and length in inches.",
         {"type": "object", "properties": {}}, get_size_chart, frozenset({BROWSING, ORDER_CREATED, SIZING})),
    Tool("start_order",
         "Start an order for a design when she says she wants to order/buy it. Tells you whether she has saved "
         "measurements with us. Then guide her through size, options and address one step at a time.",
         {"type": "object", "properties": {"design_code": _CODE}, "required": ["design_code"]}, start_order, SHOPPING),
    # Fabric only (bought on the shop website)
    Tool("search_fabrics",
         "Find fabrics we sell (in stock) by words like 'madras check', 'pink cotton', 'silk': code (FAB-<id>), "
         "name, price per metre. Use it when she wants fabric/material only and the design has no fabric links, "
         "or asks what fabrics we have.",
         {"type": "object", "properties": {"query": {"type": "string"},
          "limit": {"type": "integer", "minimum": 1, "maximum": MAX_FABRICS}}, "required": ["query"]},
         search_fabrics, SHOPPING),
    Tool("get_fabric",
         "Look up one fabric by its code (FAB-6223): name, price per metre, description, in stock.",
         {"type": "object", "properties": {"code": _FAB}, "required": ["code"]}, get_fabric),
    Tool("send_fabric_photos",
         "Send her up to 3 photos of a fabric (FAB- code), captioned with name and price per metre.",
         {"type": "object", "properties": {"code": _FAB, "max_photos": {"type": "integer", "minimum": 1,
          "maximum": MAX_PHOTOS}}, "required": ["code"]}, send_fabric_photos, SHOPPING),
    Tool("send_fabric_links",
         "When she wants only the fabric (not stitched): send the fabric(s) as messages with photo, price and a "
         "'Buy on srisafabrics.com' button. design_code = all fabrics linked to that design (get_design shows "
         "them as fabric_only); fabric_code = one fabric from search_fabrics. She buys on the website.",
         {"type": "object", "properties": {"design_code": _CODE, "fabric_code": _FAB}}, send_fabric_links, SHOPPING),
    # Collections & goods cart (fabric by the metre, readymade by size)
    Tool("get_collection",
         "Look up a fabric or readymade collection (one Instagram post with several items) by its code (e.g. MCS-1) or "
         "by an item code (e.g. MCS-1B): items with code, colour, price per metre/piece, in stock, sizes in stock "
         "(readymade), max metres (fabric) and instructions. Use it before saying anything about a collection.",
         {"type": "object", "properties": {"code": {"type": "string", "description": "Collection or item code, e.g. MCS-1 or MCS-1B."}},
          "required": ["code"]}, get_collection),
    Tool("send_collection_photos",
         "Send her the collection's item photos, each captioned with its code, colour and price (all, or only the given "
         "item codes). Then ask which code(s) she likes.",
         {"type": "object", "properties": {"code": {"type": "string"}, "item_codes": {"type": "array", "items": {"type": "string"}}},
          "required": ["code"]}, send_collection_photos, SHOPPING | frozenset({CUSTOMISE})),
    Tool("set_cart_item",
         "Put an item in her cart or change its quantity (0 removes it): code = collection item code (e.g. MCS-1B) or "
         "FAB-<id> from search_fabrics, or product_id (a design's fabric_only product). quantity = metres for fabric "
         "(from 1, steps of 0.25) or pieces for readymade; size = one of the item's sizes_in_stock (readymade). Each "
         "item+size is one line; call once per line. mybill checks stock and sizes.",
         {"type": "object", "properties": {"code": {"type": "string"}, "product_id": {"type": "integer"},
          "quantity": {**_NUM, "description": "metres (fabric) or pieces (readymade)"}, "size": {"type": "string"}},
          "required": ["quantity"]}, set_cart_item, SHOPPING | frozenset({CUSTOMISE, ADDRESS, CONFIRM})),
    Tool("remove_cart_item",
         "Remove a line from her cart (item code or product_id, plus size for readymade).",
         {"type": "object", "properties": {"code": {"type": "string"}, "product_id": {"type": "integer"},
          "size": {"type": "string"}}}, remove_cart_item, frozenset({CUSTOMISE, ADDRESS, CONFIRM})),
    Tool("finish_cart",
         "She has everything she wants in the cart: move on to the delivery address (or straight to the summary if "
         "the address is already saved).",
         {"type": "object", "properties": {}}, finish_cart, frozenset({CUSTOMISE})),
    # Sizing
    Tool("send_size_options",
         "Send her a tappable list of our sizes (with bust/waist/hip). Prefer this to typing the chart out.",
         {"type": "object", "properties": {}}, send_size_options, frozenset({SIZING})),
    Tool("send_fit_options",
         "Send her Snug / Regular / Relaxed fit buttons.",
         {"type": "object", "properties": {}}, send_fit_options, frozenset({SIZING})),
    Tool("save_size",
         "Save her size from our chart, her height in cm, and/or fit preference (SNUG, REGULAR or RELAXED). Any of "
         "them can be given alone. Once size, height and fit are all known, the order moves on to options. "
         "size_code CUSTOM = custom size: our tailor calls her after payment for exact measurements (no height/fit needed).",
         {"type": "object", "properties": {"size_code": {"type": "string", "description": "e.g. M"},
          "height_cm": {**_NUM, "description": "120-200"},
          "fit_preference": {"type": "string", "enum": list(FITS)}}}, save_size, frozenset({SIZING})),
    Tool("save_custom_measurements",
         "Save body measurements she gives in inches (bust 24-60, waist 20-56, hip 26-64) and any tailoring notes. "
         "Convert centimetres to inches first (divide by 2.54).",
         {"type": "object", "properties": {"bust_in": _NUM, "waist_in": _NUM, "hip_in": _NUM,
          "notes": {"type": "string"}}}, save_custom_measurements, frozenset({SIZING})),
    Tool("use_saved_measurements",
         "Reuse the measurements she saved with us before (only if start_order said she has them and she agrees).",
         {"type": "object", "properties": {}}, use_saved_measurements, frozenset({SIZING})),
    # Customise
    Tool("send_customisation_options",
         "Send her tappable choices for one option group of the design (e.g. SLEEVE), showing extra charges.",
         {"type": "object", "properties": {"group": {"type": "string", "description": "e.g. SLEEVE"}},
          "required": ["group"]}, send_customisation_options, frozenset({CUSTOMISE})),
    Tool("choose_option",
         "Record her choice for an option group (option_id from get_design). Replaces any earlier choice in the "
         "same group.",
         {"type": "object", "properties": {"option_id": {"type": "integer"}}, "required": ["option_id"]},
         choose_option, frozenset({CUSTOMISE})),
    Tool("set_quantity",
         "Set how many pieces she wants (1-5; more is a bulk order for the team).",
         {"type": "object", "properties": {"qty": {"type": "integer", "minimum": 1, "maximum": flow.MAX_QTY}},
          "required": ["qty"]}, set_quantity, frozenset({CUSTOMISE})),
    Tool("finish_customisation",
         "Move on to the delivery address once she has chosen every option group of the design.",
         {"type": "object", "properties": {}}, finish_customisation, frozenset({CUSTOMISE})),
    # Address
    Tool("save_delivery_details",
         "Save the delivery name and address (line1, optional line2, city, state, 6-digit pincode, optional phone - "
         "defaults to her WhatsApp number). Checks delivery to the pincode and prepares the price.",
         {"type": "object", "properties": {
             "name": {"type": "string"}, "line1": {"type": "string"}, "line2": {"type": "string"},
             "city": {"type": "string"}, "state": {"type": "string"}, "pincode": {"type": "string"},
             "phone": {"type": "string"}},
          "required": ["name", "line1", "city", "state", "pincode"]}, save_delivery_details, frozenset({ADDRESS})),
    # Confirm
    Tool("send_order_summary",
         "Send her the final order summary (fresh price, delivery date) with Confirm and Change buttons.",
         {"type": "object", "properties": {}}, send_order_summary, frozenset({CONFIRM})),
    # Change (going back)
    Tool("change_size", "Go back to change her size, height, fit or measurements.",
         {"type": "object", "properties": {}}, _go_back(SIZING), frozenset({CUSTOMISE, ADDRESS, CONFIRM})),
    Tool("change_option", "Go back to change options or quantity.",
         {"type": "object", "properties": {}}, _go_back(CUSTOMISE), frozenset({ADDRESS, CONFIRM})),
    Tool("change_address", "Go back to change the delivery address or phone.",
         {"type": "object", "properties": {}}, _go_back(ADDRESS), frozenset({CONFIRM})),
    # Instagram only (orders are taken on WhatsApp)
    Tool("send_whatsapp_order_link",
         "Instagram only. Orders are placed on WhatsApp: when she wants to order or buy, asks how to order, wants a "
         "price for her size, delivery, payment or custom changes, send this 'Order on WhatsApp' button for the "
         "design (ask which design first if it isn't clear). Also use it to resend the link.",
         {"type": "object", "properties": {"design_code": {"type": "string",
          "description": "Design code (KUR-101), collection code (MCS-1) or item code (MCS-1B)."}}}, send_whatsapp_order_link),
]

# Instagram is for discovery: browsing tools + the WhatsApp order link. Everything else is WhatsApp only.
INSTAGRAM_TOOLS = frozenset({"get_design", "list_latest_designs", "send_design_photos", "set_current_design",
                             "search_fabrics", "get_fabric", "send_fabric_photos", "send_fabric_links",
                             "get_collection", "send_collection_photos",
                             "get_size_chart", "request_human_help", "send_whatsapp_order_link"})
INSTAGRAM_ONLY = frozenset({"send_whatsapp_order_link"})

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def allowed(tool: Tool, stage: str, channel: str = "whatsapp") -> bool:
    if channel == "instagram":
        return tool.name in INSTAGRAM_TOOLS
    return tool.name not in INSTAGRAM_ONLY and tool.allowed_in(stage)


def tools_for_stage(stage: str, channel: str = "whatsapp") -> list[Tool]:
    """Tools offered in a stage on a channel, in a fixed order (so each prefix stays identical for caching)."""
    return [t for t in TOOLS if allowed(t, stage, channel)]


def tool_definitions(stage: str = BROWSING, channel: str = "whatsapp") -> list[dict[str, Any]]:
    return [t.definition() for t in tools_for_stage(stage, channel)]


async def run_tool(ctx: ToolContext, name: str, args: Any) -> tuple[dict[str, Any], bool, int]:
    """Execute one tool call -> (result, is_error, duration_ms). Never raises. Re-checks the stage."""
    started = time.monotonic()
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        result, is_error = {"error": "unknown_tool", "name": name}, True
    elif not allowed(tool, ctx.stage, ctx.conversation.channel or "whatsapp"):
        result, is_error = {"error": "not_allowed_in_stage", "stage": ctx.stage, "tool": name}, True
        if ctx.conversation.channel == "instagram":
            result["hint"] = "On Instagram, orders are placed on WhatsApp: use send_whatsapp_order_link."
    elif not isinstance(args, dict):
        result, is_error = {"error": "invalid_input"}, True
    else:
        try:
            result = await tool.handler(ctx, args)
            is_error = False
        except Exception:  # a bug in a handler must not kill the conversation
            logger.exception("Tool failed", extra={"fields": {"tool": name}})
            result, is_error = {"error": "temporarily_unavailable"}, True
    return result, is_error, int((time.monotonic() - started) * 1000)

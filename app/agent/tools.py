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

from app.agent import flow, orders
from app.agent.flow import ADDRESS, BROWSING, CONFIRM, CUSTOMISE, ORDER_CREATED, SIZING
from app.agent.formatting import format_rupees
from app.models import DRAFT_CANCELLED, DRAFT_OPEN, Conversation, OrderDraft
from app.mybill_client import MybillClient, MybillNotFound, MybillRejected, MybillUnavailable

logger = logging.getLogger(__name__)

MAX_PHOTOS = 3
MAX_LATEST = 6
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
    if draft and draft.design_code:
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
        result["note"] = "Her order is created. Payment link: our team will send it shortly."
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
        return {"unit": "inches (body measurements)", "sizes": await ctx.chart()}
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
        customer = await ctx.mybill.upsert_customer(orders.phone_e164(ctx.conversation), ctx.conversation.customer_name)
        saved = await ctx.mybill.get_latest_measurements(customer["customer_id"])
    except (MybillUnavailable, MybillRejected):
        return _unavailable()

    draft = ctx.draft if ctx.draft is not None and ctx.draft.status == DRAFT_OPEN else None
    if draft is None:
        draft = await ctx.store.new_draft(ctx.conversation)
        ctx.draft = draft
    elif draft.design_code != design["code"]:
        draft.option_ids = []  # options belong to the old design
    draft.design_code = design["code"]
    draft.mybill_customer_id = customer["customer_id"]
    ctx.conversation.mybill_customer_id = customer["customer_id"]
    ctx.conversation.current_product_code = design["code"]
    flow.touch(draft)
    flow.set_stage(ctx.conversation, SIZING if flow.missing_for(SIZING, draft) else CUSTOMISE)
    ctx.actions.append({"action": "order_started", "code": design["code"]})

    result: dict[str, Any] = {"ok": True, "design": {"code": design["code"], "name": design["name"]},
                              "has_saved_measurements": saved is not None}
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
    ok = await ctx.messenger.send_list("Please choose your size 👇 (body measurements in inches)", "Choose size",
                                       [{"title": "Sizes", "rows": rows}])
    return {"sent": ok, "sizes": [s["size_code"] for s in chart]} if ok else {"error": "send_failed"}


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
        try:
            valid = [s["size_code"] for s in await ctx.chart()]
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
        quote = await ctx.mybill.quote(orders.quote_request(draft))
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
    design = await _fetch_design(ctx, ctx.draft.design_code)
    if "error" in design:
        return design
    try:
        return await orders.send_summary(ctx.conversation, ctx.draft, ctx.mybill, ctx.messenger, design)
    except MybillUnavailable:
        return _unavailable()
    except MybillRejected as exc:
        return {"error": "quote_rejected", "code": exc.code, "message": exc.message}


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


# ── registry ─────────────────────────────────────────────────────────────────

_CODE = {"type": "string", "description": "Design code exactly as shown to the customer, e.g. KUR-101."}
_NUM = {"type": "number"}
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
         "Our size chart (XS-XXL) with body bust, waist, hip, shoulder and length in inches.",
         {"type": "object", "properties": {}}, get_size_chart, frozenset({BROWSING, ORDER_CREATED, SIZING})),
    Tool("start_order",
         "Start an order for a design when she says she wants to order/buy it. Tells you whether she has saved "
         "measurements with us. Then guide her through size, options and address one step at a time.",
         {"type": "object", "properties": {"design_code": _CODE}, "required": ["design_code"]}, start_order, SHOPPING),
    # Sizing
    Tool("send_size_options",
         "Send her a tappable list of our sizes (with bust/waist/hip). Prefer this to typing the chart out.",
         {"type": "object", "properties": {}}, send_size_options, frozenset({SIZING})),
    Tool("send_fit_options",
         "Send her Snug / Regular / Relaxed fit buttons.",
         {"type": "object", "properties": {}}, send_fit_options, frozenset({SIZING})),
    Tool("save_size",
         "Save her size from our chart, her height in cm, and/or fit preference (SNUG, REGULAR or RELAXED). Any of "
         "them can be given alone. Once size, height and fit are all known, the order moves on to options.",
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
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tools_for_stage(stage: str) -> list[Tool]:
    """Tools offered in a stage, in a fixed order (so each stage's cached prefix stays identical)."""
    return [t for t in TOOLS if t.allowed_in(stage)]


def tool_definitions(stage: str = BROWSING) -> list[dict[str, Any]]:
    return [t.definition() for t in tools_for_stage(stage)]


async def run_tool(ctx: ToolContext, name: str, args: Any) -> tuple[dict[str, Any], bool, int]:
    """Execute one tool call -> (result, is_error, duration_ms). Never raises. Re-checks the stage."""
    started = time.monotonic()
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        result, is_error = {"error": "unknown_tool", "name": name}, True
    elif not tool.allowed_in(ctx.stage):
        result, is_error = {"error": "not_allowed_in_stage", "stage": ctx.stage, "tool": name}, True
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

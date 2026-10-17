"""The agent's tools: JSON schema for the model + an async handler each.

Identity is never a tool input: the customer, her phone number and the conversation come from
ToolContext, and prices only ever come from mybill. Handlers never raise for expected problems;
they return {"error": "not_found" | "temporarily_unavailable" | "invalid_input"} so the model can
respond as the system prompt instructs.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.formatting import format_rupees
from app.models import Conversation
from app.mybill_client import MybillClient, MybillNotFound, MybillUnavailable

logger = logging.getLogger(__name__)

MAX_PHOTOS = 3
MAX_LATEST = 6
HANDOFF_MESSAGE = "A member of our team will reply to you here soon."

# (to, image_url, caption) -> WhatsApp message id, or None if sending failed.
ImageSender = Callable[[str, str, str | None], Awaitable[str | None]]


@dataclass
class ToolContext:
    """Everything a tool may touch. conversation is mutated in place; the worker's session persists it."""

    conversation: Conversation
    mybill: MybillClient | None
    send_image: ImageSender
    actions: list[dict[str, Any]] = field(default_factory=list)  # side effects, reported in AgentResult


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _code(value: Any) -> str | None:
    """Normalise a design code like ' kur-101 ' -> 'KUR-101'; None if missing."""
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


async def _fetch_design(ctx: ToolContext, code: str | None) -> dict[str, Any]:
    """mybill design JSON, or {"error": ...}."""
    if not code:
        return {"error": "invalid_input", "detail": "code is required, e.g. KUR-101"}
    if ctx.mybill is None:
        return {"error": "temporarily_unavailable"}
    try:
        return await ctx.mybill.get_design(code)
    except MybillNotFound:
        return {"error": "not_found", "code": code}
    except MybillUnavailable:
        return {"error": "temporarily_unavailable"}


async def get_design(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    design = await _fetch_design(ctx, _code(args.get("code")))
    if "error" in design:
        return design
    return {
        "code": design["code"],
        "name": design["name"],
        "base_price": design["base_price"],
        "currency": design.get("currency", "INR"),
        "description": design.get("description"),
        "fabric": design.get("fabric_description"),
        "lead_time_days": design.get("lead_time_days"),
        "option_groups": [
            {"group": g["group"], "options": [{"name": o["name"], "price_delta": o["price_delta"]} for o in g["options"]]}
            for g in design.get("option_groups", [])
        ],
        "image_count": len(design.get("images", [])),
    }


async def list_latest_designs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.mybill is None:
        return {"error": "temporarily_unavailable"}
    try:
        designs = await ctx.mybill.list_latest_designs(_int(args.get("limit"), 4, 1, MAX_LATEST))
    except MybillUnavailable:
        return {"error": "temporarily_unavailable"}
    return {"designs": [
        {"code": d["code"], "name": d["name"], "base_price": d["base_price"], "has_photo": bool(d.get("image_url"))}
        for d in designs
    ]}


async def get_size_chart(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.mybill is None:
        return {"error": "temporarily_unavailable"}
    try:
        return {"unit": "inches (body measurements)", "sizes": await ctx.mybill.get_size_chart()}
    except MybillUnavailable:
        return {"error": "temporarily_unavailable"}


async def send_design_photos(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    design = await _fetch_design(ctx, _code(args.get("code")))
    if "error" in design:
        return design
    images = design.get("images", [])[: _int(args.get("max_photos"), MAX_PHOTOS, 1, MAX_PHOTOS)]
    if not images:
        return {"sent": 0, "code": design["code"], "note": "this design has no photos yet"}
    caption = f"{design['name']} – {format_rupees(design.get('base_price'))}"
    to = ctx.conversation.external_user_id  # always the conversation's customer, never a tool input
    sent = 0
    for i, image in enumerate(images):
        if await ctx.send_image(to, image["url"], caption if i == 0 else None):
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


async def request_human_help(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason") or "unspecified").strip()[:200]
    ctx.conversation.needs_human = True
    ctx.conversation.needs_human_reason = reason
    ctx.actions.append({"action": "human_handoff", "reason": reason})
    logger.warning("HUMAN_HANDOFF", extra={"fields": {
        "conversation_id": str(ctx.conversation.id), "reason": reason,
        "customer": ctx.conversation.external_user_id,
    }})
    return {"ok": True, "tell_customer": HANDOFF_MESSAGE,
            "note": "The team now handles this chat; you will not see her next messages."}


_CODE_PROP = {"type": "string", "description": "Design code exactly as shown to the customer, e.g. KUR-101."}

TOOLS: list[Tool] = [
    Tool(
        name="get_design",
        description=(
            "Look up one of our designs by its code (like KUR-101) and get its real details: name, base price in "
            "rupees, fabric, stitching/lead time in days, the customisation options with their extra charges, and "
            "how many photos we have. Use this every time the customer mentions or asks about a specific design, "
            "before you say anything about its price, fabric, options or timing. Returns {\"error\": \"not_found\"} "
            "if we have no such design."
        ),
        input_schema={"type": "object", "properties": {"code": _CODE_PROP}, "required": ["code"]},
        handler=get_design,
    ),
    Tool(
        name="list_latest_designs",
        description=(
            "Get our newest designs (code, name, base price, whether a photo exists). Use this when the customer "
            "wants to see what's new or hasn't said which design she likes, then ask which one interests her."
        ),
        input_schema={"type": "object", "properties": {"limit": {
            "type": "integer", "minimum": 1, "maximum": MAX_LATEST,
            "description": "How many designs to fetch (default 4, at most 6)."}}},
        handler=list_latest_designs,
    ),
    Tool(
        name="get_size_chart",
        description=(
            "Get our standard size chart: for each size code (XS to XXL) the body bust, waist, hip, shoulder and "
            "length in inches. Use it when the customer asks about sizes or which size would fit her."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=get_size_chart,
    ),
    Tool(
        name="send_design_photos",
        description=(
            "Send the customer up to 3 photos of a design on WhatsApp, captioned with its name and price. Use it "
            "when she asks to see a design. The photos go to the customer you are chatting with; returns how many "
            "were sent. After calling it, just tell her the photos are on the way - don't describe them."
        ),
        input_schema={"type": "object", "properties": {
            "code": _CODE_PROP,
            "max_photos": {"type": "integer", "minimum": 1, "maximum": MAX_PHOTOS,
                           "description": "How many photos to send (default 3, at most 3)."}},
            "required": ["code"]},
        handler=send_design_photos,
    ),
    Tool(
        name="set_current_design",
        description=(
            "Remember which design the customer is currently interested in (it is checked against our catalog). "
            "Call it together with get_design whenever she settles on or asks about a specific design."
        ),
        input_schema={"type": "object", "properties": {"code": _CODE_PROP}, "required": ["code"]},
        handler=set_current_design,
    ),
    Tool(
        name="request_human_help",
        description=(
            "Hand this chat to a member of our shop team. Use it when she wants to order (reason \"ready_to_order\"), "
            "asks for a person, is upset or complaining, asks about returns, damaged items, bulk/wholesale or "
            "custom designs, when a tool reports temporarily_unavailable, or whenever you don't know an answer. "
            "After calling it, tell her warmly that a team member will reply here soon."
        ),
        input_schema={"type": "object", "properties": {"reason": {
            "type": "string",
            "description": "Short reason, e.g. ready_to_order, wants_person, complaint, temporarily_unavailable."}},
            "required": ["reason"]},
        handler=request_human_help,
    ),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tool_definitions() -> list[dict[str, Any]]:
    """Deterministic list (same bytes every request, so the prompt cache holds)."""
    return [t.definition() for t in TOOLS]


async def run_tool(ctx: ToolContext, name: str, args: Any) -> tuple[dict[str, Any], bool, int]:
    """Execute one tool call -> (result, is_error, duration_ms). Never raises."""
    started = time.monotonic()
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        result, is_error = {"error": "unknown_tool", "name": name}, True
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

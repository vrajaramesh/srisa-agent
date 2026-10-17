"""Order tools, stage gating, confirmation rules and interactive messages - no database (memory store, respx mybill)."""

import uuid
from typing import Any

import httpx
import pytest
import respx

from app import whatsapp_client
from app.agent import flow, orders, tools
from app.agent.local import MemoryStore, RecordingMessenger
from app.models import Conversation, Message
from app.mybill_client import MybillClient, static_token_provider
from app.webhook_parser import parse_webhook
from tests import mybill_mock as mm


@pytest.fixture
async def ctx(router: respx.MockRouter):
    mock = mm.MybillMock(router)
    async with httpx.AsyncClient() as http:
        conv = Conversation(id=uuid.uuid4(), channel="whatsapp", external_user_id="919876543210",
                            customer_name="Asha", stage="browsing", needs_human=False)
        c = tools.ToolContext(conversation=conv, mybill=MybillClient("https://mybill.test", static_token_provider(None), http),
                              messenger=RecordingMessenger(), store=MemoryStore())
        c.mock = mock  # type: ignore[attr-defined]
        yield c


async def call(c: tools.ToolContext, tool: str, **args: Any) -> dict[str, Any]:
    return (await tools.run_tool(c, tool, args))[0]


async def to_stage(c: tools.ToolContext, stage: str) -> None:
    """Drive the real tools up to `stage`."""
    await call(c, "start_order", design_code="KUR-101")
    if stage == flow.SIZING:
        return
    await call(c, "save_size", size_code="M", height_cm=160, fit_preference="REGULAR")
    if stage == flow.CUSTOMISE:
        return
    await call(c, "choose_option", option_id=mm.NECK_ROUND)
    await call(c, "choose_option", option_id=mm.SLEEVE_FULL)
    await call(c, "finish_customisation")
    if stage == flow.ADDRESS:
        return
    await call(c, "save_delivery_details", name="Asha Rao", line1="12 MG Road", city="Hyderabad",
               state="Telangana", pincode="500001")
    assert c.stage == flow.CONFIRM


# ── Gating ───────────────────────────────────────────────────────────────────

def test_tools_offered_per_stage() -> None:
    names = lambda s: {t.name for t in tools.tools_for_stage(s)}  # noqa: E731
    always = {"get_design", "get_order_progress", "cancel_order_draft", "request_human_help"}
    assert always <= names(flow.BROWSING) and always <= names(flow.CONFIRM)
    assert "start_order" in names(flow.BROWSING) and "start_order" not in names(flow.SIZING)
    assert {"save_size", "send_size_options", "use_saved_measurements"} <= names(flow.SIZING)
    assert "choose_option" in names(flow.CUSTOMISE) and "choose_option" not in names(flow.ADDRESS)
    assert names(flow.CONFIRM) >= {"send_order_summary", "change_size", "change_option", "change_address"}
    assert "send_order_summary" not in names(flow.ADDRESS)


async def test_disallowed_tool_in_wrong_stage_is_rejected(ctx) -> None:
    result, is_error, _ = await tools.run_tool(ctx, "save_delivery_details", {"name": "x"})
    assert is_error and result == {"error": "not_allowed_in_stage", "stage": "browsing", "tool": "save_delivery_details"}
    assert (await call(ctx, "send_order_summary"))["error"] == "not_allowed_in_stage"


# ── Browsing → sizing → customise ────────────────────────────────────────────

async def test_start_order_creates_draft_and_moves_to_sizing(ctx) -> None:
    result = await call(ctx, "start_order", design_code="kur-101")
    assert result["ok"] and result["has_saved_measurements"] is False and result["stage"] == flow.SIZING
    assert ctx.draft.design_code == "KUR-101" and ctx.draft.mybill_customer_id == 512
    assert ctx.conversation.mybill_customer_id == 512 and "customer_id" not in str(result)
    again = await call(ctx, "start_order", design_code="KUR-101")  # gated: already ordering
    assert again["error"] == "not_allowed_in_stage" and len(ctx.store.drafts) == 1


async def test_start_order_reports_saved_measurements(router) -> None:
    mock = mm.MybillMock(router, saved_measurements={"size_code": "L", "height_cm": 165.0, "fit_preference": "SNUG"})
    async with httpx.AsyncClient() as http:
        conv = Conversation(id=uuid.uuid4(), external_user_id="919876543210", stage="browsing", needs_human=False)
        c = tools.ToolContext(conversation=conv, mybill=MybillClient("https://mybill.test", static_token_provider(None), http),
                              messenger=RecordingMessenger(), store=MemoryStore())
        result = await call(c, "start_order", design_code="KUR-101")
        assert result["has_saved_measurements"] and result["saved_measurements"]["size_code"] == "L"
        moved = await call(c, "use_saved_measurements")
        assert moved["stage"] == flow.CUSTOMISE and c.draft.size_code == "L"
        assert mock.measurement_posts  # saved back to mybill when sizing completed


async def test_sizing_validation_and_advance(ctx) -> None:
    await to_stage(ctx, flow.SIZING)
    assert (await call(ctx, "save_size", size_code="XXXL"))["error"] == "invalid_size"
    assert (await call(ctx, "save_size", height_cm=90))["error"] == "out_of_range"
    assert (await call(ctx, "save_custom_measurements", bust_in=92))["error"] == "out_of_range"
    partial = await call(ctx, "save_size", size_code="m", height_cm=160)
    assert partial["stage"] == flow.SIZING and partial["missing_fields"][0] == "fit_preference"
    done = await call(ctx, "save_size", fit_preference="relaxed")
    assert done["stage"] == flow.CUSTOMISE and done["measurements_saved"] is True
    assert ctx.mock.measurement_posts[-1]["size_code"] == "M"


async def test_size_list_message(ctx) -> None:
    await to_stage(ctx, flow.SIZING)
    await call(ctx, "send_size_options")
    sent = ctx.messenger.of_kind("list")[-1]
    rows = sent["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["size:S", "size:M", "size:L"]
    assert rows[1]["description"] == "Bust 36 · Waist 30 · Hip 39 in"


# ── Option rules ─────────────────────────────────────────────────────────────

async def test_option_rules(ctx) -> None:
    await to_stage(ctx, flow.CUSTOMISE)
    assert (await call(ctx, "choose_option", option_id=mm.OTHER_DESIGN_OPTION))["error"] == "invalid_option"
    assert (await call(ctx, "choose_option", option_id=mm.INACTIVE_OPTION))["error"] == "invalid_option"
    await call(ctx, "choose_option", option_id=mm.SLEEVE_HALF)
    replaced = await call(ctx, "choose_option", option_id=mm.SLEEVE_FULL)   # same group -> replaces
    assert replaced["replaced_previous_choice"] and ctx.draft.option_ids == [mm.SLEEVE_FULL]
    incomplete = await call(ctx, "finish_customisation")
    assert incomplete["error"] == "incomplete" and incomplete["missing_fields"] == ["option:NECK"]
    await call(ctx, "choose_option", option_id=mm.NECK_BOAT)
    assert (await call(ctx, "set_quantity", qty=9))["error"] == "out_of_range"
    assert (await call(ctx, "finish_customisation"))["stage"] == flow.ADDRESS


async def test_option_buttons_show_price_deltas(ctx) -> None:
    await to_stage(ctx, flow.CUSTOMISE)
    await call(ctx, "send_customisation_options", group="sleeve")
    buttons = ctx.messenger.of_kind("buttons")[-1]["buttons"]
    assert buttons == [{"id": f"opt:{mm.SLEEVE_HALF}", "title": "Half sleeve"},
                       {"id": f"opt:{mm.SLEEVE_FULL}", "title": "Full sleeve +₹150"}]


# ── Address → summary ────────────────────────────────────────────────────────

async def test_address_validation_and_quote(ctx) -> None:
    await to_stage(ctx, flow.ADDRESS)
    bad = await call(ctx, "save_delivery_details", name="Asha", line1="1 Rd", city="Hyd", state="TS", pincode="012345")
    assert bad["error"] == "invalid_input" and any("pincode" in m for m in bad["missing_or_invalid"])
    ok = await call(ctx, "save_delivery_details", name="Asha", line1="1 Rd", city="Hyd", state="TS", pincode="500001")
    assert ok["stage"] == flow.CONFIRM and ctx.draft.delivery_phone == "+919876543210"  # defaults to WhatsApp number
    assert ctx.draft.last_quote["breakdown"]["subtotal"] == 2049.0
    assert ctx.draft.last_quote["breakdown"]["total"] == 2149.0


async def test_summary_message(ctx) -> None:
    await to_stage(ctx, flow.CONFIRM)
    result = await call(ctx, "send_order_summary")
    assert result["sent"] and ctx.draft.summary_sent_version == ctx.draft.version
    msg = ctx.messenger.of_kind("buttons")[-1]
    assert [b["id"] for b in msg["buttons"]] == [f"confirm:{ctx.draft.id}:{ctx.draft.version}", f"change:{ctx.draft.id}"]
    assert msg["meta"] == {"kind": "order_summary", "draft_id": str(ctx.draft.id), "version": ctx.draft.version}
    for expected in ("Anarkali Kurta (KUR-101)", "Size M · Height 160 cm · Regular fit", "Round neck (included)",
                     "Full sleeve (+₹150)", "Subtotal ₹2,049 · Delivery ₹100", "*Total ₹2,149*", "Estimated delivery"):
        assert expected in msg["text"]


async def test_change_tools_move_back_and_bump_version(ctx) -> None:
    await to_stage(ctx, flow.CONFIRM)
    await call(ctx, "send_order_summary")
    v = ctx.draft.version
    result = await call(ctx, "change_option")
    assert result["stage"] == flow.CUSTOMISE and ctx.draft.version == v + 1 and ctx.draft.summary_sent_version is None


# ── Confirmation (code path) ─────────────────────────────────────────────────

async def _confirm(c: tools.ToolContext, reply_id: str | None = None, text: str | None = None,
                   last: Message | None = None) -> orders.ConfirmOutcome | None:
    pending = [Message(direction="in", type="interactive" if reply_id else "text", text=text, reply_id=reply_id)]
    found = orders.find_confirmation(pending, last if last is not None else
                                     (c.messenger.messages[-1] if c.messenger.messages else None), c.draft)
    if found is None:
        return None
    return await orders.confirm(c.conversation, c.draft, found, c.store, c.mybill, c.messenger, c.design)


async def test_confirm_creates_exactly_one_order(ctx) -> None:
    await to_stage(ctx, flow.CONFIRM)
    await call(ctx, "send_order_summary")
    draft = ctx.draft
    tap = f"confirm:{draft.id}:{draft.version}"
    outcome = await _confirm(ctx, reply_id=tap)
    assert outcome.kind == "created" and outcome.order_no == "SB-1001"
    assert len(ctx.mock.create_calls) == 1
    body = ctx.mock.create_calls[0]
    assert body["idempotency_key"] == f"{draft.id}:{draft.version}" and body["customer_id"] == 512
    assert body["measurements"] == {"size_code": "M", "height_cm": 160.0, "fit_preference": "REGULAR"}
    assert draft.status == "submitted" and draft.mybill_order_no == "SB-1001"
    assert ctx.conversation.stage == flow.ORDER_CREATED
    assert ctx.messenger.of_kind("text")[-1]["text"] == \
        "🎉 Order SB-1001 created! Total ₹2,149. We'll send your payment link shortly."

    ctx.draft = None  # what the worker loads next time: no open draft
    texts_before = len(ctx.messenger.of_kind("text"))
    double = await _confirm(ctx, reply_id=tap)    # double tap
    assert double.kind == "already_submitted" and len(ctx.mock.create_calls) == 1
    assert len(ctx.messenger.of_kind("text")) == texts_before   # no second confirmation message


async def test_yes_after_summary_confirms(ctx) -> None:
    await to_stage(ctx, flow.CONFIRM)
    await call(ctx, "send_order_summary")
    assert (await _confirm(ctx, text="Haan 👍".split()[0])).kind == "created"


async def test_stale_version_resends_summary_without_ordering(ctx) -> None:
    await to_stage(ctx, flow.CONFIRM)
    await call(ctx, "send_order_summary")
    old_tap = f"confirm:{ctx.draft.id}:{ctx.draft.version}"
    await call(ctx, "change_address")
    await call(ctx, "save_delivery_details", name="Asha", line1="2 New Rd", city="Hyd", state="TS", pincode="500002")
    outcome = await _confirm(ctx, reply_id=old_tap)
    assert outcome.kind == "resent_summary" and ctx.mock.create_calls == []
    latest = ctx.messenger.of_kind("buttons")[-1]
    assert latest["text"].startswith("Your order details changed") and "2 New Rd" in latest["text"]
    assert ctx.draft.summary_sent_version == ctx.draft.version


async def test_yes_without_summary_is_not_a_confirmation(ctx) -> None:
    await to_stage(ctx, flow.CONFIRM)
    await ctx.messenger.send_text("Shall I send the summary?")
    assert await _confirm(ctx, text="yes") is None
    assert ctx.mock.create_calls == []


async def test_mybill_down_at_confirmation(ctx) -> None:
    await to_stage(ctx, flow.CONFIRM)
    await call(ctx, "send_order_summary")
    ctx.mock.unavailable = True
    outcome = await _confirm(ctx, reply_id=f"confirm:{ctx.draft.id}:{ctx.draft.version}")
    assert outcome.kind == "unavailable" and ctx.draft.status == "open"


@pytest.mark.parametrize("text,yes", [("yes", True), ("OK!", True), ("avunu", True), ("👍", True), ("haan.", True),
                                      ("yes but change size", False), ("no", False), ("", False)])
def test_affirmatives(text: str, yes: bool) -> None:
    assert orders.is_affirmative(text) is yes


# ── Interactive messages ─────────────────────────────────────────────────────

def _webhook(message: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "919876543210", "profile": {"name": "Asha"}}],
                                              "messages": [{"from": "919876543210", "id": "wamid.x", **message}]}}]}]}


def test_parse_button_and_list_replies() -> None:
    button = parse_webhook(_webhook({"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": "confirm:abc:3", "title": "✅ Confirm"}}})).messages[0]
    assert (button.type, button.reply_id, button.text) == ("interactive", "confirm:abc:3", "✅ Confirm")
    row = parse_webhook(_webhook({"type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": "size:M", "title": "M", "description": "Bust 36"}}})).messages[0]
    assert (row.reply_id, row.text) == ("size:M", "M")
    legacy = parse_webhook(_webhook({"type": "button", "button": {"payload": "show_designs", "text": "Show designs"}})).messages[0]
    assert (legacy.reply_id, legacy.text) == ("show_designs", "Show designs")


def test_whatsapp_limits_are_enforced() -> None:
    payload = whatsapp_client.build_buttons("b" * 2000, [{"id": f"id{i}", "title": "A very long button title here"}
                                                         for i in range(5)])
    buttons = payload["action"]["buttons"]
    assert len(buttons) == 3 and all(len(b["reply"]["title"]) <= 20 for b in buttons)
    assert len(payload["body"]["text"]) == 1024
    lst = whatsapp_client.build_list("body", "Choose your size please", [
        {"title": "A", "rows": [{"id": f"r{i}", "title": "T" * 40, "description": "D" * 100} for i in range(8)]},
        {"title": "B", "rows": [{"id": f"s{i}", "title": "x"} for i in range(8)]}])
    rows = [r for s in lst["action"]["sections"] for r in s["rows"]]
    assert len(rows) == 10 and len(lst["action"]["button"]) <= 20
    assert all(len(r["title"]) <= 24 and len(r.get("description", "")) <= 72 for r in rows)
    assert whatsapp_client.clip("✅ Confirm order now!!", 10).endswith("…")

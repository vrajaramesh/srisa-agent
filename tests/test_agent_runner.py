"""The agent loop, context, tools, resolution and splitting - no database involved (fake LLM, respx mybill)."""

import json
import uuid
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from app.agent import context, resolution, tools
from app.agent.local import MemoryStore, RecordingMessenger
from app.agent.formatting import format_rupees, split_message
from app.agent.runner import run_agent
from app.config import get_settings
from app.models import Conversation, Message
from app.mybill_client import MybillClient, static_token_provider
from tests import fake_llm as llm
from tests.conftest import MYBILL

DESIGN = {
    "code": "KUR-101", "name": "Anarkali Kurta", "garment_type": "Kurta", "description": "Flared",
    "fabric_description": "Rayon crepe", "base_price": 1899.00, "lead_time_days": 7, "currency": "INR",
    "images": [{"url": f"https://img.test/{i}.jpg", "sort_order": i} for i in range(4)],
    "option_groups": [{"group": "SLEEVE", "options": [
        {"option_id": 4, "name": "Full sleeve", "price_delta": 150.0, "applies_to_all_designs": True}]}],
}


def conversation(**kw: Any) -> Conversation:
    kw.setdefault("stage", "browsing")
    return Conversation(id=uuid.uuid4(), channel="whatsapp", external_user_id="919876543210",
                        customer_name="Asha", needs_human=False, **kw)


def inbound(text: str | None, type_: str = "text", raw: dict | None = None) -> Message:
    return Message(direction="in", type=type_, text=text, raw=raw, status="received")


def outbound(text: str) -> Message:
    return Message(direction="out", type="text", text=text, status="sent")


@pytest.fixture
async def mybill(router: respx.MockRouter):
    router.get(f"{MYBILL}/designs/KUR-101").mock(return_value=Response(200, json=DESIGN))
    router.get(f"{MYBILL}/designs/NOP-1").mock(return_value=Response(404, json={"code": "DESIGN_NOT_FOUND"}))
    router.get(f"{MYBILL}/designs/latest").mock(return_value=Response(200, json=[
        {"code": "KUR-101", "name": "Anarkali Kurta", "base_price": 1899.0, "currency": "INR", "image_url": "u"}]))
    router.get(f"{MYBILL}/size-chart").mock(return_value=Response(503))
    async with httpx.AsyncClient() as http:
        yield MybillClient("https://mybill.test", static_token_provider(None), http)


def ctx(conv: Conversation, mybill: MybillClient | None) -> tools.ToolContext:
    return tools.ToolContext(conversation=conv, mybill=mybill, messenger=RecordingMessenger(), store=MemoryStore())


# ── Loop ─────────────────────────────────────────────────────────────────────

async def test_tool_loop_runs_tool_then_answers(fake_llm, mybill) -> None:
    conv = conversation()
    fake_llm.script = [
        llm.tool_use("get_design", {"code": "kur-101"}, cache_write=1500),
        llm.text("Anarkali Kurta is ₹1,899 😊", cache_read=1500),
    ]
    result = await run_agent(conv, [inbound("price of KUR-101?")], ctx(conv, mybill), get_settings())

    assert result.replies == ["Anarkali Kurta is ₹1,899 😊"] and not result.fallback
    assert [c["name"] for c in result.tool_calls] == ["get_design"]
    assert (result.input_tokens, result.cache_read_tokens, result.cache_write_tokens) == (200, 1500, 1500)

    second = fake_llm.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0].type == "tool_use"   # echoed unchanged
    tool_result = second[2]["content"][0]
    assert tool_result["tool_use_id"] == "toolu_get_design" and "is_error" not in tool_result
    payload = json.loads(tool_result["content"])
    assert payload["base_price"] == 1899.0 and payload["image_count"] == 4
    assert payload["option_groups"][0]["options"][0] == {"option_id": 4, "name": "Full sleeve", "price_delta": 150.0}


async def test_request_shape_caches_static_prompt_and_tools(fake_llm, mybill) -> None:
    conv = conversation(current_product_code="KUR-101")
    await run_agent(conv, [inbound("hi")], ctx(conv, mybill), get_settings())
    await run_agent(conv, [inbound("hi"), outbound("hello"), inbound("new?")], ctx(conv, mybill), get_settings())
    first, second = fake_llm.calls

    assert first["model"] == "claude-sonnet-5-5"
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["output_config"] == {"effort": "low"} and first["max_tokens"] == 600
    static, facts = first["system"]
    assert static["cache_control"] == {"type": "ephemeral"} and "SRISA Boutique" in static["text"]
    assert "{BRAND_NAME}" not in static["text"]
    assert "cache_control" not in facts and "KUR-101" in facts["text"] and "Asia/Kolkata" in facts["text"]
    # Cached prefix (tools + static prompt) must be byte-identical across turns.
    assert first["tools"] == second["tools"] and first["system"][0] == second["system"][0]
    assert [t["name"] for t in first["tools"]] == [t.name for t in tools.tools_for_stage("browsing")]


async def test_stops_at_tool_iteration_limit(fake_llm, mybill) -> None:
    conv = conversation()
    fake_llm.script = [llm.tool_use("get_design", {"code": "KUR-101"}, block_id=f"t{i}") for i in range(20)]
    settings = get_settings()
    result = await run_agent(conv, [inbound("hi")], ctx(conv, mybill), settings)
    assert result.fallback and result.error == "tool_iteration_limit"
    assert len(fake_llm.calls) == settings.llm_max_tool_iterations + 1
    assert len(result.tool_calls) == settings.llm_max_tool_iterations


async def test_timeout_is_retried_once(fake_llm, mybill) -> None:
    conv = conversation()
    fake_llm.script = [llm.timeout_error(), llm.text("Hello!")]
    result = await run_agent(conv, [inbound("hi")], ctx(conv, mybill), get_settings())
    assert result.replies == ["Hello!"] and len(fake_llm.calls) == 2


async def test_second_timeout_falls_back(fake_llm, mybill) -> None:
    conv = conversation()
    fake_llm.script = [llm.timeout_error(), llm.timeout_error()]
    result = await run_agent(conv, [inbound("hi")], ctx(conv, mybill), get_settings())
    assert result.fallback and result.error == "APITimeoutError" and result.replies == []


async def test_refusal_falls_back(fake_llm, mybill) -> None:
    conv = conversation()
    fake_llm.script = [llm.response(stop_reason="refusal", category="general_harms")]
    result = await run_agent(conv, [inbound("hi")], ctx(conv, mybill), get_settings())
    assert result.fallback and result.error == "refusal:general_harms"


async def test_max_tokens_retries_with_more_room(fake_llm, mybill) -> None:
    conv = conversation()
    fake_llm.script = [llm.response(stop_reason="max_tokens"), llm.text("Done")]
    result = await run_agent(conv, [inbound("hi")], ctx(conv, mybill), get_settings())
    assert result.replies == ["Done"] and fake_llm.calls[1]["max_tokens"] == 1200


async def test_handoff_without_text_uses_handoff_message(fake_llm, mybill) -> None:
    conv = conversation()
    fake_llm.script = [llm.tool_use("request_human_help", {"reason": "ready_to_order"}), llm.response()]
    result = await run_agent(conv, [inbound("I want to order")], ctx(conv, mybill), get_settings())
    assert result.replies == [tools.HANDOFF_MESSAGE]
    assert conv.needs_human and conv.needs_human_reason == "ready_to_order"
    assert result.actions == [{"action": "human_handoff", "reason": "ready_to_order"}]


# ── Tools ────────────────────────────────────────────────────────────────────

async def test_model_cannot_override_customer_identity(mybill) -> None:
    conv = conversation()
    c = ctx(conv, mybill)
    result, is_error, _ = await tools.run_tool(
        c, "send_design_photos",
        {"code": "KUR-101", "to": "+911111111111", "phone": "911111111111", "customer_id": 99, "max_photos": 9})
    assert not is_error and result["sent"] == 3
    images = c.messenger.of_kind("image")   # the Messenger only ever sends to the conversation's customer
    assert len(images) == 3 and images[0]["caption"] == "Anarkali Kurta – ₹1,899" and images[1]["caption"] is None
    # No tool schema even offers identity or price fields.
    for tool in tools.TOOLS:
        assert not {"to", "customer_id", "price", "total", "wa_id", "idempotency_key"} & \
            set(tool.input_schema.get("properties", {}))


async def test_tool_errors_are_returned_not_raised(mybill) -> None:
    c = ctx(conversation(), mybill)
    assert (await tools.run_tool(c, "get_design", {"code": "NOP-1"}))[0] == {"error": "not_found", "code": "NOP-1"}
    assert (await tools.run_tool(c, "get_size_chart", {}))[0] == {"error": "temporarily_unavailable"}
    assert (await tools.run_tool(c, "get_design", {}))[0]["error"] == "invalid_input"
    assert (await tools.run_tool(c, "no_such_tool", {}))[1] is True
    assert (await tools.run_tool(ctx(conversation(), None), "list_latest_designs", {}))[0] == \
        {"error": "temporarily_unavailable"}


async def test_set_current_design_validates(mybill) -> None:
    conv = conversation()
    c = ctx(conv, mybill)
    assert (await tools.run_tool(c, "set_current_design", {"code": "NOP-1"}))[0]["error"] == "not_found"
    assert conv.current_product_code is None
    await tools.run_tool(c, "set_current_design", {"code": "kur-101"})
    assert conv.current_product_code == "KUR-101"


# ── Product resolution ───────────────────────────────────────────────────────

async def test_design_code_in_text_sets_current_design(mybill) -> None:
    conv = conversation()
    assert await resolution.resolve_design_code(conv, ["hi", "is kur-101 available in cotton?"], mybill) == "KUR-101"
    assert conv.current_product_code == "KUR-101"


async def test_unknown_design_code_is_ignored(mybill) -> None:
    conv = conversation(current_product_code="KUR-101")
    assert await resolution.resolve_design_code(conv, ["what about NOP-1"], mybill) is None
    assert conv.current_product_code == "KUR-101"


def test_find_design_codes() -> None:
    assert resolution.find_design_codes("KUR-101 vs leh-6, not AB-1 or ABCD-2") == ["KUR-101", "LEH-6"]


# ── Context ──────────────────────────────────────────────────────────────────

def test_history_merges_roles_and_starts_with_customer() -> None:
    history = [outbound("old greeting"), inbound("hi"), inbound(None, "image"), outbound("Hello!"),
               outbound("Here are designs"), inbound(None, "audio")]
    assert context.build_messages(history) == [
        {"role": "user", "content": "hi\n[customer sent an image]"},
        {"role": "assistant", "content": "Hello!\nHere are designs"},
        {"role": "user", "content": "[customer sent a voice note]"},
    ]


def test_buttons_and_captions_reach_the_model() -> None:
    assert context.message_text(inbound("Show designs", "button")) == "[tapped: Show designs]"
    assert context.message_text(inbound("✅ Confirm", "interactive")) == "[tapped: ✅ Confirm]"
    assert context.message_text(inbound(None, "image", {"image": {"caption": "like this"}})) == \
        "[customer sent an image with the caption: like this]"


def test_facts_block() -> None:
    order = {"stage": "sizing", "draft": {"design_code": "LEH-1", "size_code": "M"},
             "missing_fields": ["height_cm", "fit_preference"], "summary_awaiting_confirmation": False}
    facts = context.facts_block(conversation(current_product_code="LEH-1"), order, now=datetime(2026, 10, 17, 20, 0))
    assert "Asha" in facts and "LEH-1" in facts and "Order stage: sizing" in facts
    assert "height_cm, fit_preference" in facts and "awaiting her confirmation: no" in facts and "free-form" in facts


# ── Formatting ───────────────────────────────────────────────────────────────

def test_short_message_is_not_split() -> None:
    assert split_message("Hello 😊") == ["Hello 😊"]


def test_long_message_splits_at_paragraphs_within_limit() -> None:
    paragraphs = [("Para %d. " % i) + "x" * 380 for i in range(5)]
    chunks = split_message("\n\n".join(paragraphs))
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) == 3
    assert chunks[0] == "\n\n".join(paragraphs[:2])


def test_huge_paragraph_splits_at_sentences_then_hard_cut() -> None:
    text = " ".join(["This is a sentence."] * 120) + " " + "y" * 2500
    chunks = split_message(text)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_format_rupees() -> None:
    assert format_rupees(1899.0) == "₹1,899" and format_rupees("1299.50") == "₹1,299.50"

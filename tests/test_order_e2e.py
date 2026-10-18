"""End-to-end: WhatsApp webhooks -> worker -> scripted Claude + tools -> order_created (real Postgres, respx mybill/Meta)."""

import json
import uuid

import respx
from fastapi.testclient import TestClient

from tests import fake_llm as llm
from tests import mybill_mock as mm
from tests.conftest import TEST_SCHEMA, run_sql, sign

PHONE = "919876543210"


def post(client: TestClient, message: dict) -> None:
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "106540352242922"},
        "contacts": [{"profile": {"name": "Asha Rao"}, "wa_id": PHONE}],
        "messages": [{"from": PHONE, "id": f"wamid.{uuid.uuid4().hex}", "timestamp": "1760700000", **message}]}}]}]}
    body = json.dumps(payload).encode()
    resp = client.post("/webhook/whatsapp", content=body,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200


def say(client: TestClient, text: str) -> None:
    post(client, {"type": "text", "text": {"body": text}})


def tap(client: TestClient, reply_id: str, title: str, kind: str = "button_reply") -> None:
    post(client, {"type": "interactive", "interactive": {"type": kind, kind: {"id": reply_id, "title": title}}})


def sent(graph_api: respx.Route) -> list[dict]:
    return [json.loads(c.request.content) for c in graph_api.calls if b'"status"' not in c.request.content]


def stage() -> str:
    return run_sql(f"SELECT stage FROM {TEST_SCHEMA}.conversations")[0]["stage"]


def draft() -> dict:
    return run_sql(f"SELECT id, status, version, summary_sent_version, size_code, height_cm, fit_preference, "
                   f"option_ids, mybill_order_no FROM {TEST_SCHEMA}.order_drafts ORDER BY created_at DESC LIMIT 1")[0]


def test_whatsapp_conversation_to_order_created(client: TestClient, router: respx.MockRouter,
                                                 graph_api: respx.Route, fake_llm) -> None:
    mybill = mm.MybillMock(router)
    address = {"name": "Asha Rao", "line1": "12 MG Road", "city": "Hyderabad", "state": "Telangana", "pincode": "500001"}
    fake_llm.script = [
        # 1. "I want KUR-101"
        llm.tool_use("start_order", {"design_code": "KUR-101"}, block_id="t1"),
        llm.tool_use("send_size_options", {}, block_id="t2"),
        llm.text("Lovely choice! 😊 Please pick your size from the list."),
        # 2. taps size M (applied by code) -> model asks the next question
        llm.text("Got it, size M. What is your height in cm, and do you like a snug, regular or relaxed fit?"),
        # 3. "160 cm, regular"
        llm.tool_use("save_size", {"height_cm": 160, "fit_preference": "REGULAR"}, block_id="t3"),
        llm.tool_use("send_customisation_options", {"group": "NECK"}, block_id="t4"),
        llm.text("Perfect. Which neckline would you like?"),
        # 4. taps Round neck
        llm.tool_use("send_customisation_options", {"group": "SLEEVE"}, block_id="t5"),
        llm.text("And the sleeves?"),
        # 5. taps Full sleeve
        llm.tool_use("finish_customisation", {}, block_id="t6"),
        llm.text("Great! Please share your delivery address with pincode."),
        # 6. address
        llm.tool_use("save_delivery_details", address, block_id="t7"),
        llm.tool_use("send_order_summary", {}, block_id="t8"),
        llm.text("Here is your order summary - please tap Confirm if everything looks right."),
    ]

    say(client, "Hi, I want KUR-101")
    assert stage() == "sizing"
    size_list = [m for m in sent(graph_api) if m.get("type") == "interactive"][-1]
    assert size_list["interactive"]["type"] == "list"

    tap(client, "size:M", "M", kind="list_reply")
    assert draft()["size_code"] == "M" and stage() == "sizing"
    assert fake_llm.calls[-1]["messages"][-1]["content"].endswith("[tapped: M]")

    say(client, "160 cm, regular fit please")
    assert stage() == "customise" and mybill.measurement_posts[-1]["height_cm"] == 160.0

    tap(client, f"opt:{mm.NECK_ROUND}", "Round neck")
    tap(client, f"opt:{mm.SLEEVE_FULL}", "Full sleeve +₹150")
    assert stage() == "address" and sorted(draft()["option_ids"]) == sorted([mm.SLEEVE_FULL, mm.NECK_ROUND])

    say(client, "Asha Rao, 12 MG Road, Hyderabad, Telangana 500001")
    d = draft()
    assert stage() == "confirm" and d["summary_sent_version"] == d["version"]
    summary = [m for m in sent(graph_api) if m.get("type") == "interactive"][-1]["interactive"]
    assert summary["type"] == "button" and "*Total ₹2,149*" in summary["body"]["text"]
    confirm_id = summary["action"]["buttons"][0]["reply"]["id"]
    assert confirm_id == f"confirm:{d['id']}:{d['version']}"

    llm_calls = len(fake_llm.calls)
    tap(client, confirm_id, "✅ Confirm")
    assert len(fake_llm.calls) == llm_calls            # confirmation never goes through the model
    assert len(mybill.create_calls) == 1
    assert mybill.create_calls[0]["idempotency_key"] == f"{d['id']}:{d['version']}"
    assert stage() == "order_created"
    assert draft()["status"] == "submitted" and draft()["mybill_order_no"] == "SB-1001"
    texts = [m["text"]["body"] for m in sent(graph_api) if m.get("type") == "text"]
    assert texts[-1].startswith("🎉 Order SB-1001 created! Total ₹2,149.") and "https://rzp.io/i/SB-1001" in texts[-1]

    before = len(sent(graph_api))
    tap(client, confirm_id, "✅ Confirm")              # double tap
    assert len(mybill.create_calls) == 1 and len(sent(graph_api)) == before
    turns = run_sql(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.llm_turns")[0]["n"]
    assert turns == 6


def test_stale_confirm_after_change_resends_summary(client: TestClient, router: respx.MockRouter,
                                                    graph_api: respx.Route, fake_llm, monkeypatch) -> None:
    monkeypatch.setenv("LLM_MAX_TOOL_ITERATIONS", "10")
    from app.config import get_settings

    get_settings.cache_clear()
    mybill = mm.MybillMock(router)
    address = {"name": "Asha", "line1": "1 Rd", "city": "Hyd", "state": "TS", "pincode": "500001"}
    fake_llm.script = [
        llm.tool_use("start_order", {"design_code": "KUR-101"}, block_id="a"),
        llm.tool_use("save_size", {"size_code": "M", "height_cm": 160, "fit_preference": "SNUG"}, block_id="b"),
        llm.tool_use("choose_option", {"option_id": mm.NECK_ROUND}, block_id="c"),
        llm.tool_use("choose_option", {"option_id": mm.SLEEVE_HALF}, block_id="d"),
        llm.tool_use("finish_customisation", {}, block_id="e"),
        llm.tool_use("save_delivery_details", address, block_id="f"),
        llm.tool_use("send_order_summary", {}, block_id="g"),
        llm.text("Please confirm 😊"),
        # She changes the size; the model does NOT re-send the summary.
        llm.tool_use("change_size", {}, block_id="h"),
        llm.tool_use("save_size", {"size_code": "L"}, block_id="i"),
        llm.text("Updated to size L."),
    ]
    say(client, "I want KUR-101, size M, 160cm, snug, round neck, half sleeve. Asha, 1 Rd, Hyd, TS 500001")
    old = draft()
    old_confirm = f"confirm:{old['id']}:{old['version']}"
    assert stage() == "confirm" and old["summary_sent_version"] == old["version"]

    say(client, "Actually make it size L")
    assert draft()["size_code"] == "L" and draft()["summary_sent_version"] is None

    calls = len(fake_llm.calls)
    tap(client, old_confirm, "✅ Confirm")             # stale tap
    assert mybill.create_calls == [] and len(fake_llm.calls) == calls
    resent = [m for m in sent(graph_api) if m.get("type") == "interactive"][-1]["interactive"]
    assert resent["body"]["text"].startswith("Your order details changed") and "Size L" in resent["body"]["text"]
    new = draft()
    assert stage() == "confirm" and new["summary_sent_version"] == new["version"] > old["version"]

    tap(client, resent["action"]["buttons"][0]["reply"]["id"], "✅ Confirm")
    assert len(mybill.create_calls) == 1 and mybill.create_calls[0]["size_code"] == "L"
    assert stage() == "order_created"

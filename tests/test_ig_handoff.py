"""Instagram -> WhatsApp handoff on the WhatsApp side: the pre-typed message with "ref IG-XXXXX" links the
accounts (mybill upsert with phone + Instagram id), resolves the design and starts like the ad flow."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import respx
from fastapi.testclient import TestClient

from app.agent import ig_handoff
from tests import fake_llm as llm
from tests import mybill_mock as mm
from tests.conftest import TEST_SCHEMA, run_sql, sign

PHONE = "919876543210"
IGSID = "1000000000000001"


def say(client: TestClient, text: str) -> None:
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "106540352242922"},
        "contacts": [{"profile": {"name": "Asha Rao"}, "wa_id": PHONE}],
        "messages": [{"from": PHONE, "id": f"wamid.{uuid.uuid4().hex}",
                      "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                      "type": "text", "text": {"body": text}}]}}]}]}
    body = json.dumps(payload).encode()
    assert client.post("/webhook/whatsapp", content=body,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)}).status_code == 200


def seed_ref(code: str = "IG-7K3QA", design: str | None = "KUR-101", expires_in_days: int = 7) -> str:
    run_sql(f"INSERT INTO {TEST_SCHEMA}.ref_codes (code, ig_user_id, design_code, expires_at) VALUES "
            f"('{code}', '{IGSID}', {repr(design) if design else 'NULL'}, now() + interval '{expires_in_days} days')")
    return code


def sent(graph_api: respx.Route) -> list[dict]:
    return [json.loads(c.request.content) for c in graph_api.calls if b'"status"' not in c.request.content]


def test_prefilled_text_and_link() -> None:
    text = ig_handoff.prefilled_text("KUR-101", "IG-7K3QA")
    assert text == "Hi SRISA! I'd like to order KUR-101 🛍️ (ref IG-7K3QA)"
    assert ig_handoff.whatsapp_link("+91 94916 47691", text).startswith("https://wa.me/919491647691?text=Hi%20SRISA")
    assert ig_handoff.REF_RE.search(text).group(1) == "IG-7K3QA"
    assert ig_handoff.REF_RE.search("ref ig-7k3qa").group(1).upper() == "IG-7K3QA"
    assert all(ch not in ig_handoff.new_ref_code()[3:] for ch in "01OIL")


def test_handoff_links_accounts_and_starts_with_the_design(client: TestClient, router: respx.MockRouter,
                                                           graph_api: respx.Route, fake_llm) -> None:
    mybill = mm.MybillMock(router)
    ref = seed_ref()
    say(client, f"Hi SRISA! I'd like to order KUR-101 🛍️ (ref {ref})")

    image, buttons = sent(graph_api)
    assert image["type"] == "image" and image["image"]["caption"].startswith("Hi Asha! 👋 Thanks for checking out this design.")
    assert [b["reply"]["id"] for b in buttons["interactive"]["action"]["buttons"]] == [
        "ad:order:KUR-101", "ad:photos:KUR-101", "ad:sizes"]
    assert fake_llm.calls == []
    assert mybill.upserts[-1] == {"phone_e164": "+" + PHONE, "instagram_user_id": IGSID, "name": "Asha Rao"}
    code, = run_sql(f"SELECT wa_id, used_at FROM {TEST_SCHEMA}.ref_codes")
    assert code["wa_id"] == PHONE and code["used_at"] is not None
    conv, = run_sql(f"SELECT entry_source_type, entry_source_id, entry_resolution, entry_label, current_product_code, "
                    f"mybill_customer_id, fep_expires_at FROM {TEST_SCHEMA}.conversations")
    assert conv == {"entry_source_type": "instagram", "entry_source_id": IGSID, "entry_resolution": "ig_handoff",
                    "entry_label": f"Instagram DM ({ref})", "current_product_code": "KUR-101",
                    "mybill_customer_id": 512, "fep_expires_at": None}

    # She taps "Order this": the normal WhatsApp order flow continues with the model.
    fake_llm.script = [llm.text("Lovely! Which size do you usually wear?")]
    payload_tap = {"type": "interactive", "interactive": {"type": "button_reply",
                                                          "button_reply": {"id": "ad:order:KUR-101", "title": "🛍️ Order this"}}}
    body = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "106540352242922"},
        "contacts": [{"profile": {"name": "Asha Rao"}, "wa_id": PHONE}],
        "messages": [{"from": PHONE, "id": f"wamid.{uuid.uuid4().hex}", "timestamp": str(int(datetime.now().timestamp())),
                      **payload_tap}]}}]}]}
    raw = json.dumps(body).encode()
    client.post("/webhook/whatsapp", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(raw)})
    assert run_sql(f"SELECT stage FROM {TEST_SCHEMA}.conversations")[0]["stage"] == "sizing"
    assert "came from our Instagram DMs to order on WhatsApp (design KUR-101)" in fake_llm.calls[-1]["system"][1]["text"]


def test_design_without_code_in_ref_uses_text_and_unknown_ref_still_works(client: TestClient, router: respx.MockRouter,
                                                                          graph_api: respx.Route, fake_llm) -> None:
    mybill = mm.MybillMock(router)
    say(client, "Hi SRISA! I'd like to order KUR-101 🛍️ (ref IG-ZZZZZ)")   # unknown ref: no link, design from text
    assert [b["type"] for b in sent(graph_api)] == ["image", "interactive"]
    assert mybill.upserts == [] and fake_llm.calls == []
    conv, = run_sql(f"SELECT entry_source_type, current_product_code FROM {TEST_SCHEMA}.conversations")
    assert conv == {"entry_source_type": None, "current_product_code": "KUR-101"}


def test_expired_ref_is_not_linked(client: TestClient, router: respx.MockRouter, graph_api: respx.Route,
                                   fake_llm) -> None:
    mybill = mm.MybillMock(router)
    ref = seed_ref(expires_in_days=-1)
    say(client, f"Hi! I'd like to order KUR-101 (ref {ref})")
    assert mybill.upserts == []
    assert run_sql(f"SELECT wa_id FROM {TEST_SCHEMA}.ref_codes")[0]["wa_id"] is None


def test_returning_customer_gets_the_model_with_the_design(client: TestClient, router: respx.MockRouter,
                                                            graph_api: respx.Route, fake_llm) -> None:
    mm.MybillMock(router)
    say(client, "hello")                                  # chatted before (within 7 days)
    ref = seed_ref()
    fake_llm.script = [llm.text("Great choice! Shall we start your order for the Anarkali Kurta?")]
    say(client, f"Hi SRISA! I'd like to order KUR-101 🛍️ (ref {ref})")
    assert sent(graph_api)[-1]["text"]["body"] == "Great choice! Shall we start your order for the Anarkali Kurta?"
    assert run_sql(f"SELECT current_product_code FROM {TEST_SCHEMA}.conversations")[0]["current_product_code"] == "KUR-101"

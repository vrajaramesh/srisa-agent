"""Staff inbox: handover -> alert -> list/detail -> staff reply from the shop's number -> resolve -> bot again."""

import json
import uuid
from datetime import datetime, timezone

import pytest
import respx
from fastapi.testclient import TestClient

from app.config import get_settings
from tests import fake_llm as llm
from tests import mybill_mock as mm
from tests.conftest import TEST_SCHEMA, run_sql, sent_texts, sign

PHONE = "919876543210"
ADMIN = {"Authorization": "Bearer test-admin-key"}


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


def bodies(route: respx.Route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls if b'"status"' not in c.request.content]


@pytest.fixture
def alerts_on(monkeypatch) -> None:
    monkeypatch.setenv("STAFF_ALERT_NUMBERS", "+91 94412 00001, 919441200002")
    monkeypatch.setenv("MYBILL_UI_URL", "https://mybill.test/")
    get_settings.cache_clear()


def test_staff_api_requires_auth(client: TestClient) -> None:
    assert client.get("/staff/conversations").status_code == 403
    assert client.get("/staff/conversations", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/staff/conversations/count", headers=ADMIN).json() == {"waiting": 0}


def test_handover_alert_reply_resolve(client: TestClient, router: respx.MockRouter, graph_api: respx.Route,
                                      fake_llm, alerts_on) -> None:
    mm.MybillMock(router)
    fake_llm.script = [llm.tool_use("request_human_help", {"reason": "damaged_item"}),
                       llm.text("I'm so sorry 🙏 A member of our team will reply to you here soon.")]
    say(client, "My lehenga hem is torn 😞")

    sent = bodies(graph_api)
    assert sent[0]["type"] == "text" and sent[0]["to"] == PHONE                    # the bot tells her
    alerts = [b for b in sent if b["to"] in ("919441200001", "919441200002")]
    assert [a["to"] for a in alerts] == ["919441200001", "919441200002"] and alerts[0]["type"] == "text"
    conv_id = run_sql(f"SELECT id FROM {TEST_SCHEMA}.conversations")[0]["id"]
    assert alerts[0]["text"]["body"] == ("🔔 SRISA staff alert\n\nCustomer: Asha Rao\nContact: +" + PHONE +
                                        "\nAction needed: needs help – damaged item\n\nPlease follow up with the "
                                        f"customer and update the order in mybill: https://mybill.test/?chat={conv_id}")

    say(client, "Please help, I need it on Sunday")                                 # bot stays silent
    assert len(fake_llm.calls) == 2 and len(bodies(graph_api)) == 3

    waiting = client.get("/staff/conversations", headers=ADMIN).json()
    assert len(waiting) == 1
    row = waiting[0]
    assert row["id"] == str(conv_id) and row["needs_human_reason"] == "damaged_item" and row["phone"] == "+" + PHONE
    assert row["waiting_messages"] == 1 and row["reply_window_open"] is True and row["needs_human_since"]
    assert row["last_message"]["text"] == "Please help, I need it on Sunday"
    assert client.get("/staff/conversations/count", headers=ADMIN).json() == {"waiting": 1}

    detail = client.get(f"/staff/conversations/{conv_id}", headers=ADMIN).json()
    assert [(m["author"], m["text"][:12]) for m in detail["messages"]] == [
        ("customer", "My lehenga h"), ("bot", "I'm so sorry"), ("customer", "Please help,")]

    r = client.post(f"/staff/conversations/{conv_id}/reply", headers=ADMIN,
                    json={"text": "Hi Asha, this is Ramesh from SRISA. We'll pick it up tomorrow 🙏", "staff_name": "ramesh"})
    assert r.status_code == 200
    assert sent_texts(graph_api)[-1] == "Hi Asha, this is Ramesh from SRISA. We'll pick it up tomorrow 🙏"
    staff_row = run_sql(f"SELECT author FROM {TEST_SCHEMA}.messages WHERE direction = 'out' ORDER BY created_at DESC LIMIT 1")
    assert staff_row == [{"author": "staff"}]

    assert client.post(f"/staff/conversations/{conv_id}/keep", headers=ADMIN).json() == {
        "needs_human": True, "needs_human_reason": "damaged_item"}                  # unchanged
    assert client.post(f"/staff/conversations/{conv_id}/resolve", headers=ADMIN).json() == {
        "needs_human": False, "needs_human_reason": None}
    assert client.get("/staff/conversations", headers=ADMIN).json() == []

    fake_llm.script = [llm.text("So happy to hear that, Asha! 😊")]
    say(client, "Got it back, looks perfect!")                                     # the bot answers again
    assert sent_texts(graph_api)[-1] == "So happy to hear that, Asha! 😊"


def test_staff_reply_to_active_chat_turns_bot_off_without_alert(client: TestClient, router: respx.MockRouter,
                                                                graph_api: respx.Route, fake_llm, alerts_on) -> None:
    say(client, "hi")
    conv_id = run_sql(f"SELECT id FROM {TEST_SCHEMA}.conversations")[0]["id"]
    r = client.post(f"/staff/conversations/{conv_id}/reply", headers=ADMIN, json={"text": "Hello from the team"})
    assert r.status_code == 200
    conv = run_sql(f"SELECT needs_human, needs_human_reason FROM {TEST_SCHEMA}.conversations")[0]
    assert conv == {"needs_human": True, "needs_human_reason": "staff_reply"}
    assert not [b for b in bodies(graph_api) if b["type"] == "template"]


def test_reply_window_closed(client: TestClient, graph_api: respx.Route, fake_llm) -> None:
    say(client, "hi")
    run_sql(f"UPDATE {TEST_SCHEMA}.conversations SET last_customer_message_at = now() - interval '25 hours'")
    conv_id = run_sql(f"SELECT id FROM {TEST_SCHEMA}.conversations")[0]["id"]
    r = client.post(f"/staff/conversations/{conv_id}/reply", headers=ADMIN, json={"text": "Hello?"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "WINDOW_CLOSED"
    assert client.get(f"/staff/conversations/{uuid.uuid4()}", headers=ADMIN).status_code == 404


def test_no_alert_numbers_means_no_template(client: TestClient, router: respx.MockRouter, graph_api: respx.Route,
                                           fake_llm) -> None:
    fake_llm.script = [llm.tool_use("request_human_help", {"reason": "wants_person"}), llm.text("Sure, a team member will reply.")]
    say(client, "I want to talk to a person")
    assert not [b for b in bodies(graph_api) if b["type"] == "template"]
    assert run_sql(f"SELECT needs_human_since IS NOT NULL AS stamped FROM {TEST_SCHEMA}.conversations")[0]["stamped"]


def test_staff_phone_never_gets_bot_replies(client: TestClient, graph_api: respx.Route, fake_llm, monkeypatch) -> None:
    monkeypatch.setenv("STAFF_ALERT_NUMBERS", PHONE)   # this sender is a staff phone
    get_settings.cache_clear()
    say(client, "Hi")                                   # sent only to keep the alert window open
    assert fake_llm.calls == [] and bodies(graph_api) == []
    assert run_sql(f"SELECT status FROM {TEST_SCHEMA}.messages")[0]["status"] == "skipped"

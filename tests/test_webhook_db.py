"""Webhook -> database behaviour (local mode: processing runs as a BackgroundTask)."""

import copy
import json

import respx
from fastapi.testclient import TestClient

from tests.conftest import TEST_SCHEMA, load_fixture, run_sql, sent_texts, sign


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook/whatsapp",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)},
    )


def _messages() -> list[dict]:
    return run_sql(
        f"SELECT direction, external_message_id, type, text, status, raw IS NOT NULL AS has_raw "
        f"FROM {TEST_SCHEMA}.messages ORDER BY created_at"
    )


def test_text_message_stored_and_answered(client: TestClient, graph_api: respx.Route) -> None:
    assert _post(client, load_fixture("text_message.json")).status_code == 200

    conv = run_sql(f"SELECT channel, external_user_id, customer_name, stage, needs_human FROM {TEST_SCHEMA}.conversations")
    assert conv == [
        {"channel": "whatsapp", "external_user_id": "919876543210", "customer_name": "Asha Rao",
         "stage": "new", "needs_human": False}
    ]
    inbound, outbound = _messages()
    assert inbound["direction"] == "in" and inbound["status"] == "processed" and inbound["has_raw"]
    assert outbound["direction"] == "out" and outbound["status"] == "sent"
    assert outbound["text"] == "You said: Do you have red silk sarees?"
    assert outbound["external_message_id"].startswith("wamid.out")
    assert sent_texts(graph_api) == ["You said: Do you have red silk sarees?"]


def test_duplicate_webhook_stored_once(client: TestClient, graph_api: respx.Route) -> None:
    payload = load_fixture("text_message.json")
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200

    assert [m["direction"] for m in _messages()] == ["in", "out"]
    assert len(sent_texts(graph_api)) == 1


def test_batch_messages_answered_together_in_order(client: TestClient, graph_api: respx.Route) -> None:
    payload = load_fixture("text_message.json")
    value = payload["entry"][0]["changes"][0]["value"]
    first = value["messages"][0]
    value["messages"] = []
    for i, word in enumerate(["one", "two", "three"]):
        msg = copy.deepcopy(first)
        msg["id"] = f"wamid.batch{i}"
        msg["text"]["body"] = word
        value["messages"].append(msg)

    assert _post(client, payload).status_code == 200
    # Answered together in one agent run, in order.
    assert sent_texts(graph_api) == ["You said: one\ntwo\nthree"]
    assert run_sql(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.conversations") == [{"n": 1}]


def test_status_receipts_only_move_forward(client: TestClient, graph_api: respx.Route) -> None:
    assert _post(client, load_fixture("text_message.json")).status_code == 200
    out_id = _messages()[1]["external_message_id"]

    def receipt(status: str) -> dict:
        payload = load_fixture("status_only.json")
        st = payload["entry"][0]["changes"][0]["value"]["statuses"][0]
        st["id"], st["status"] = out_id, status
        return payload

    assert _post(client, receipt("read")).status_code == 200
    assert _post(client, receipt("delivered")).status_code == 200  # late, must not downgrade
    assert _messages()[1]["status"] == "read"


def test_status_for_unknown_message_ignored(client: TestClient, graph_api: respx.Route) -> None:
    assert _post(client, load_fixture("status_only.json")).status_code == 200
    assert _messages() == []
    assert graph_api.call_count == 0


def test_allowlist_stores_but_skips_other_senders(client: TestClient, graph_api: respx.Route, monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_SENDERS", "+91 98765 00000")
    from app.config import get_settings

    get_settings.cache_clear()
    assert _post(client, load_fixture("text_message.json")).status_code == 200
    assert [(m["direction"], m["status"]) for m in _messages()] == [("in", "skipped")]
    assert graph_api.call_count == 0  # neither reply nor read receipt

"""POST /internal/events: mybill payment notifications -> one WhatsApp message per event."""

import json

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app import main
from tests.conftest import MESSAGES_URL, TEST_SCHEMA, run_sql, sent_texts

EVENT = {"event": "order.paid", "order_no": "SB-1001", "customer_phone": "+919876543210", "customer_name": "Asha Rao",
         "amount": 2149.0, "estimated_delivery_date": "2026-10-30", "event_id": "evt_1"}


@pytest.fixture
def authorized(monkeypatch):
    seen = {}

    def verify(authorization, audience, service_account):
        seen.update(authorization=authorization, service_account=service_account)
        return authorization == "Bearer mybill-token"

    monkeypatch.setattr(main, "verify_tasks_token", verify)
    return seen


def _conversation() -> None:
    run_sql(f"INSERT INTO {TEST_SCHEMA}.conversations (id, channel, external_user_id, customer_name) "
            f"VALUES (gen_random_uuid(), 'whatsapp', '919876543210', 'Asha Rao')")


def _post(client: TestClient, body: dict, token: str = "mybill-token"):
    return client.post("/internal/events", json=body, headers={"Authorization": f"Bearer {token}"})


def test_paid_event_sends_one_message(client: TestClient, graph_api: respx.Route, authorized) -> None:
    _conversation()
    assert _post(client, EVENT).json() == {"status": "sent"}
    assert sent_texts(graph_api) == ["✅ Payment received for order SB-1001 (₹2,149). Thank you, Asha! "
                                     "Your outfit is now with our tailor 🧵 - estimated delivery 30 Oct."]
    assert authorized["service_account"] == "276371417285-compute@developer.gserviceaccount.com"
    assert _post(client, EVENT).json() == {"status": "duplicate"}   # mybill retry / duplicate webhook
    assert len(sent_texts(graph_api)) == 1
    out = run_sql(f"SELECT type, text FROM {TEST_SCHEMA}.messages WHERE direction = 'out'")
    assert len(out) == 1 and out[0]["text"].startswith("✅ Payment received")


def test_expired_event_offers_new_link(client: TestClient, graph_api: respx.Route, authorized) -> None:
    _conversation()
    _post(client, {**EVENT, "event": "payment_link.expired", "event_id": "evt_2"})
    assert "expired" in sent_texts(graph_api)[0] and 'Reply "pay"' in sent_texts(graph_api)[0]


def test_rejects_without_valid_token(client: TestClient, graph_api: respx.Route, authorized) -> None:
    assert client.post("/internal/events", json=EVENT).status_code == 403
    assert _post(client, EVENT, token="someone-else").status_code == 403
    assert graph_api.call_count == 0


def test_unknown_customer_is_ignored(client: TestClient, graph_api: respx.Route, authorized) -> None:
    assert _post(client, {**EVENT, "customer_phone": "+910000000000"}).json() == {"status": "unknown_customer"}
    assert graph_api.call_count == 0


def test_failed_send_is_retried_by_mybill(client: TestClient, authorized) -> None:
    _conversation()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(MESSAGES_URL).mock(return_value=Response(500, json={"error": {"code": 131000}}))
        assert _post(client, EVENT).status_code == 500
    assert run_sql(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.processed_events") == [{"n": 0}]   # not marked done
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(MESSAGES_URL).mock(return_value=Response(200, json={"messages": [{"id": "wamid.ok"}]}))
        assert _post(client, EVENT).json() == {"status": "sent"}
        assert len(route.calls) == 1


MEASURE = {**EVENT, "event_id": "evt_measure", "exact_measurements_required": True, "design_code": "BLO-12",
           "design_name": "Designer Blouse"}


def _templates(route: respx.Route) -> list[dict]:
    return [b for b in (json.loads(c.request.content) for c in route.calls) if b.get("type") == "template"]


def _alerts(route: respx.Route, number: str = "919441200001") -> list[dict]:
    return [b for b in (json.loads(c.request.content) for c in route.calls) if b.get("to") == number]


def test_paid_order_needing_exact_measurements_tells_customer_and_alerts_staff(
        client: TestClient, graph_api: respx.Route, authorized, monkeypatch) -> None:
    from app.config import get_settings
    monkeypatch.setenv("STAFF_ALERT_NUMBERS", "919441200001")
    monkeypatch.setenv("MYBILL_UI_URL", "https://mybill.test")
    get_settings.cache_clear()
    _conversation()
    assert _post(client, MEASURE).json() == {"status": "sent"}
    assert _alerts(graph_api, "919876543210")[-1]["text"]["body"].endswith(
        "\n📏 Our tailor will call you on +919876543210 shortly to take your exact measurements.")
    alert, = _alerts(graph_api)
    assert alert["type"] == "text"                               # free text first (staff messaged the shop)
    assert "Action needed: 📏 call for exact measurements – order SB-1001 (BLO-12 Designer Blouse), paid ₹2,149" \
        in alert["text"]["body"] and alert["text"]["body"].endswith("https://mybill.test/?order=SB-1001")
    assert _post(client, MEASURE).json() == {"status": "duplicate"}
    assert len(_alerts(graph_api)) == 1                          # alerted once


def test_measurement_alert_even_without_a_whatsapp_chat(client: TestClient, graph_api: respx.Route, authorized,
                                                        monkeypatch) -> None:
    from app.config import get_settings
    monkeypatch.setenv("STAFF_ALERT_NUMBERS", "919441200001")
    get_settings.cache_clear()
    assert _post(client, {**MEASURE, "event_id": "evt_m2"}).json() == {"status": "unknown_customer"}
    assert len(_alerts(graph_api)) == 1 and [b for b in _alerts(graph_api, "919876543210")] == []


def test_alert_falls_back_to_template_outside_24h(client: TestClient, router: respx.MockRouter, authorized,
                                                 monkeypatch) -> None:
    from app.config import get_settings
    monkeypatch.setenv("STAFF_ALERT_NUMBERS", "919441200001")
    get_settings.cache_clear()

    def graph(request):
        body = json.loads(request.content)
        if body.get("to") == "919441200001" and body.get("type") == "text":   # staff hasn't messaged in 24 h
            return Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
        return Response(200, json={"messages": [{"id": "wamid.x"}]})

    route = router.post(MESSAGES_URL).mock(side_effect=graph)
    assert _post(client, {**MEASURE, "event_id": "evt_m3"}).json() == {"status": "unknown_customer"}
    kinds = [b["type"] for b in _alerts(route)]
    assert kinds == ["text", "template"]
    tpl = _alerts(route)[1]["template"]
    assert tpl["name"] == "staff_alert" and [p["text"] for p in tpl["components"][0]["parameters"]][2].startswith("📏 call")

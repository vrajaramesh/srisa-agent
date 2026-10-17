import json

import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.config import get_settings
from tests.conftest import MESSAGES_URL, load_fixture, sign


def _post(client: TestClient, body: bytes, signature: str | None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook/whatsapp", content=body, headers=headers)


def _sent_bodies(route: respx.Route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


def test_valid_signature_text_message_echoes(client: TestClient, graph_api: respx.Route) -> None:
    body = json.dumps(load_fixture("text_message.json")).encode()
    resp = _post(client, body, sign(body))
    assert resp.status_code == 200

    sent = _sent_bodies(graph_api)
    assert sent[0] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": load_fixture("text_message.json")["entry"][0]["changes"][0]["value"]["messages"][0]["id"],
    }
    assert sent[1] == {
        "messaging_product": "whatsapp",
        "to": "919876543210",
        "type": "text",
        "text": {"body": "You said: Do you have red silk sarees?"},
    }
    assert graph_api.calls[1].request.headers["Authorization"] == "Bearer test-access-token"


def test_image_message_reaches_agent_as_placeholder(client: TestClient, graph_api: respx.Route, fake_llm) -> None:
    body = json.dumps(load_fixture("image_message.json")).encode()
    assert _post(client, body, sign(body)).status_code == 200
    assert fake_llm.calls[-1]["messages"][-1]["content"] == "[customer sent an image with the caption: Something like this?]"
    assert _sent_bodies(graph_api)[-1]["type"] == "text"


def test_status_only_payload_sends_nothing(client: TestClient, graph_api: respx.Route) -> None:
    body = json.dumps(load_fixture("status_only.json")).encode()
    assert _post(client, body, sign(body)).status_code == 200
    assert graph_api.call_count == 0


def test_duplicate_message_replied_once(client: TestClient, graph_api: respx.Route) -> None:
    body = json.dumps(load_fixture("text_message.json")).encode()
    assert _post(client, body, sign(body)).status_code == 200
    assert _post(client, body, sign(body)).status_code == 200
    assert graph_api.call_count == 2  # one mark_as_read + one send_text


def test_allowlist_skips_other_senders(client: TestClient, graph_api: respx.Route, monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_SENDERS", "+91 98765 00000")
    get_settings.cache_clear()
    body = json.dumps(load_fixture("text_message.json")).encode()
    assert _post(client, body, sign(body)).status_code == 200
    assert graph_api.call_count == 0  # neither reply nor read receipt


def test_allowlist_replies_to_listed_sender(client: TestClient, graph_api: respx.Route, monkeypatch) -> None:
    monkeypatch.setenv("ALLOWED_SENDERS", "911111111111, +91 98765 43210")
    get_settings.cache_clear()
    body = json.dumps(load_fixture("text_message.json")).encode()
    assert _post(client, body, sign(body)).status_code == 200
    assert graph_api.call_count == 2


def test_missing_signature_returns_401(client: TestClient, graph_api: respx.Route) -> None:
    body = json.dumps(load_fixture("text_message.json")).encode()
    assert _post(client, body, None).status_code == 401
    assert graph_api.call_count == 0


def test_invalid_signature_returns_401(client: TestClient, graph_api: respx.Route) -> None:
    body = json.dumps(load_fixture("text_message.json")).encode()
    assert _post(client, body, sign(body, secret="not-the-secret")).status_code == 401
    assert _post(client, body, "sha256=deadbeef").status_code == 401
    assert _post(client, body, "md5=abc").status_code == 401
    assert graph_api.call_count == 0


def test_tampered_body_returns_401(client: TestClient) -> None:
    body = json.dumps(load_fixture("text_message.json")).encode()
    signature = sign(body)
    assert _post(client, body.replace(b"sarees", b"lehengas"), signature).status_code == 401


def test_graph_api_error_does_not_crash(client: TestClient) -> None:
    with respx.mock() as mock:
        mock.post(MESSAGES_URL).mock(
            return_value=Response(
                400,
                json={"error": {"message": "(#131030) Recipient not in allowed list", "type": "OAuthException", "code": 131030}},
            )
        )
        body = json.dumps(load_fixture("text_message.json")).encode()
        assert _post(client, body, sign(body)).status_code == 200

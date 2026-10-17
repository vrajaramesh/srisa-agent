from fastapi.testclient import TestClient

from tests.conftest import VERIFY_TOKEN


def _handshake(client: TestClient, mode: str, token: str, challenge: str = "12345"):
    return client.get(
        "/webhook/whatsapp",
        params={"hub.mode": mode, "hub.verify_token": token, "hub.challenge": challenge},
    )


def test_correct_token_returns_challenge_as_plain_text(client: TestClient) -> None:
    resp = _handshake(client, "subscribe", VERIFY_TOKEN, "1158201444")
    assert resp.status_code == 200
    assert resp.text == "1158201444"
    assert resp.headers["content-type"].startswith("text/plain")


def test_wrong_token_returns_403(client: TestClient) -> None:
    assert _handshake(client, "subscribe", "wrong-token").status_code == 403


def test_wrong_mode_returns_403(client: TestClient) -> None:
    assert _handshake(client, "unsubscribe", VERIFY_TOKEN).status_code == 403


def test_missing_params_returns_403(client: TestClient) -> None:
    assert client.get("/webhook/whatsapp").status_code == 403

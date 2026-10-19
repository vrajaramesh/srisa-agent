"""Phase 9 step 0: Instagram webhook capture (verify handshake, signature, raw storage)."""

import json

from fastapi.testclient import TestClient

from tests.conftest import TEST_SCHEMA, VERIFY_TOKEN, run_sql, sign

DM = {"object": "instagram", "entry": [{"time": 1760700000000, "id": "17841400000000000", "messaging": [
    {"sender": {"id": "1234567890"}, "recipient": {"id": "17841400000000000"}, "timestamp": 1760700000000,
     "message": {"mid": "aWdfZAG1faXRlbToxOklHTWVzc2FnZAUlEOjE3ODQx", "text": "Hi, is this available?"}}]}]}


def test_verify_handshake(client: TestClient) -> None:
    ok = client.get("/webhook/instagram", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                                                  "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = client.get("/webhook/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                   "hub.challenge": "x"})
    assert bad.status_code == 403


def test_signed_payload_is_stored_raw(client: TestClient, monkeypatch) -> None:
    from app.config import get_settings
    monkeypatch.setenv("ALLOWED_IG_SENDERS", "999")   # not this sender: stored, but nobody replies
    get_settings.cache_clear()
    body = json.dumps(DM).encode()
    resp = client.post("/webhook/instagram", content=body,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    rows = run_sql(f"SELECT channel, payload FROM {TEST_SCHEMA}.raw_webhooks")
    assert len(rows) == 1 and rows[0]["channel"] == "instagram" and rows[0]["payload"] == DM
    # The DM is stored as a message too; the sender isn't in ALLOWED_IG_SENDERS, so it's skipped (no reply).
    msg, = run_sql(f"SELECT type, text, status FROM {TEST_SCHEMA}.messages")
    assert msg == {"type": "text", "text": "Hi, is this available?", "status": "skipped"}


def test_instagram_app_secret_is_accepted(client: TestClient, monkeypatch) -> None:
    from app.config import get_settings
    monkeypatch.setenv("IG_APP_SECRET", "ig-secret")
    monkeypatch.setenv("ALLOWED_IG_SENDERS", "999")
    get_settings.cache_clear()
    body = json.dumps(DM).encode()
    assert client.post("/webhook/instagram", content=body,
                       headers={"X-Hub-Signature-256": sign(body, "ig-secret")}).status_code == 200


def test_bad_signature_is_rejected_and_not_stored(client: TestClient) -> None:
    body = json.dumps(DM).encode()
    assert client.post("/webhook/instagram", content=body, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401
    assert client.post("/webhook/instagram", content=body).status_code == 401
    assert run_sql(f"SELECT COUNT(*) AS n FROM {TEST_SCHEMA}.raw_webhooks")[0]["n"] == 0

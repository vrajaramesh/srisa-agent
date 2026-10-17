"""/tasks/process-message: OIDC check when USE_CLOUD_TASKS=true, and Cloud Task shape."""

import json
import uuid

import pytest
import respx
from fastapi.testclient import TestClient

from app import queue, security
from app.config import get_settings

SA = "srisa-tasks@example-project.iam.gserviceaccount.com"
SERVICE_URL = "https://srisa-agent-test.a.run.app"


@pytest.fixture
def cloud_tasks_mode(monkeypatch) -> None:
    monkeypatch.setenv("USE_CLOUD_TASKS", "true")
    monkeypatch.setenv("TASKS_TARGET_URL", SERVICE_URL)
    monkeypatch.setenv("TASKS_SERVICE_ACCOUNT", SA)
    get_settings.cache_clear()


def _task(client: TestClient, headers: dict | None = None):
    return client.post("/tasks/process-message", json={"conversation_id": str(uuid.uuid4())}, headers=headers or {})


def _fake_verify(claims: dict | Exception):
    def verify(token, request, audience):
        assert audience == SERVICE_URL
        if isinstance(claims, Exception):
            raise claims
        return claims

    return verify


@pytest.mark.usefixtures("cloud_tasks_mode")
def test_rejects_missing_token(client: TestClient) -> None:
    assert _task(client).status_code == 403


@pytest.mark.usefixtures("cloud_tasks_mode")
def test_rejects_invalid_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(security.id_token, "verify_oauth2_token", _fake_verify(ValueError("bad signature")))
    assert _task(client, {"Authorization": "Bearer junk"}).status_code == 403


@pytest.mark.usefixtures("cloud_tasks_mode")
def test_rejects_token_from_other_account(client: TestClient, monkeypatch) -> None:
    claims = {"email": "someone-else@example.com", "email_verified": True}
    monkeypatch.setattr(security.id_token, "verify_oauth2_token", _fake_verify(claims))
    assert _task(client, {"Authorization": "Bearer t"}).status_code == 403


@pytest.mark.usefixtures("cloud_tasks_mode")
def test_accepts_valid_token(client: TestClient, graph_api: respx.Route, monkeypatch) -> None:
    claims = {"email": SA, "email_verified": True, "aud": SERVICE_URL}
    monkeypatch.setattr(security.id_token, "verify_oauth2_token", _fake_verify(claims))
    resp = _task(client, {"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert resp.json() == {"processed": 0, "failed": False}


def test_local_mode_needs_no_token(client: TestClient, graph_api: respx.Route) -> None:
    assert _task(client).status_code == 200


@pytest.mark.usefixtures("cloud_tasks_mode")
def test_build_task_shape() -> None:
    conv_id = uuid.uuid4()
    task = queue.build_task(conv_id)
    req = task.http_request
    assert req.url == f"{SERVICE_URL}/tasks/process-message"
    assert json.loads(req.body) == {"conversation_id": str(conv_id)}
    assert req.oidc_token.service_account_email == SA
    assert req.oidc_token.audience == SERVICE_URL
    assert not task.name  # unnamed on purpose


@pytest.mark.usefixtures("cloud_tasks_mode")
def test_enqueue_failure_still_returns_200(client: TestClient, monkeypatch) -> None:
    """Webhook saves the message and answers 200 even if Cloud Tasks is down."""
    from tests.conftest import TEST_SCHEMA, load_fixture, run_sql, sign

    async def boom(conversation_id):
        raise RuntimeError("Cloud Tasks unavailable")

    monkeypatch.setattr(queue, "_enqueue_cloud_task", boom)
    body = json.dumps(load_fixture("text_message.json")).encode()
    resp = client.post("/webhook/whatsapp", content=body,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200
    assert run_sql(f"SELECT status FROM {TEST_SCHEMA}.messages") == [{"status": "received"}]

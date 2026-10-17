"""Agent + worker + database: handoffs, fallbacks, llm_turns, photos, admin resume."""

import json
import uuid

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app import repositories as repo
from app.db import get_sessionmaker
from app.webhook_parser import IncomingMessage
from app.worker import FALLBACK_REPLY, process_conversation
from tests import fake_llm as llm
from tests.conftest import MYBILL, TEST_SCHEMA, _execute, run_sql, sent_texts
from tests.test_agent_runner import DESIGN


async def _seed(*texts: str, needs_human: bool = False) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        conv_id = None
        for i, body in enumerate(texts):
            msg = IncomingMessage(id=f"wamid.{uuid.uuid4()}", from_="919876543210", type="text",
                                  timestamp="1760700000", text=body, profile_name="Asha")
            conv_id = await repo.upsert_conversation(session, "whatsapp", msg)
            await repo.insert_inbound_message(session, conv_id, msg)
    if needs_human:
        await _execute(f"UPDATE {TEST_SCHEMA}.conversations SET needs_human = TRUE, needs_human_reason = 'complaint'")
    assert conv_id is not None
    return conv_id


async def _conversation() -> dict:
    return (await _execute(f"SELECT needs_human, needs_human_reason, current_product_code FROM {TEST_SCHEMA}.conversations"))[0]


async def _turns() -> list[dict]:
    return await _execute(f"SELECT model, input_tokens, cache_read_tokens, tool_calls, error, stop_reason "
                          f"FROM {TEST_SCHEMA}.llm_turns ORDER BY created_at")


@pytest.mark.usefixtures("db_ready")
async def test_agent_reply_photos_and_llm_turn(router: respx.MockRouter, graph_api: respx.Route, fake_llm) -> None:
    router.get(f"{MYBILL}/designs/KUR-101").mock(return_value=Response(200, json=DESIGN))
    conv_id = await _seed("show me kur-101 please")
    fake_llm.script = [
        llm.tool_use("send_design_photos", {"code": "KUR-101", "max_photos": 2}, cache_read=900),
        llm.text("Sending photos of Anarkali Kurta now 😊"),
    ]
    result = await process_conversation(conv_id)
    assert result.processed == 1

    bodies = [json.loads(c.request.content) for c in graph_api.calls]
    images = [b for b in bodies if b.get("type") == "image"]
    assert [b["image"]["link"] for b in images] == ["https://img.test/0.jpg", "https://img.test/1.jpg"]
    assert all(b["to"] == "919876543210" for b in images)
    assert sent_texts(graph_api) == ["Sending photos of Anarkali Kurta now 😊"]
    assert (await _conversation())["current_product_code"] == "KUR-101"   # resolved before the LLM

    turn, = await _turns()
    assert turn["model"] == "claude-sonnet-5-5" and turn["error"] is None and turn["stop_reason"] == "end_turn"
    assert turn["input_tokens"] == 200 and turn["cache_read_tokens"] == 900
    assert turn["tool_calls"][0]["name"] == "send_design_photos" and "duration_ms" in turn["tool_calls"][0]
    out = await _execute(f"SELECT type FROM {TEST_SCHEMA}.messages WHERE direction = 'out' ORDER BY created_at")
    assert [r["type"] for r in out] == ["image", "image", "text"]


@pytest.mark.usefixtures("db_ready")
async def test_needs_human_stops_replies(graph_api: respx.Route, fake_llm) -> None:
    conv_id = await _seed("hello?", needs_human=True)
    result = await process_conversation(conv_id)
    assert result.processed == 0 and fake_llm.calls == [] and graph_api.call_count == 0
    rows = await _execute(f"SELECT status, last_error FROM {TEST_SCHEMA}.messages")
    assert rows == [{"status": "skipped", "last_error": "needs_human"}]


@pytest.mark.usefixtures("db_ready")
async def test_llm_failure_sends_fallback_and_hands_over(graph_api: respx.Route, fake_llm) -> None:
    conv_id = await _seed("hi")
    fake_llm.script = [llm.timeout_error(), llm.timeout_error()]
    result = await process_conversation(conv_id)
    assert result.processed == 1
    assert sent_texts(graph_api) == [FALLBACK_REPLY]
    conv = await _conversation()
    assert conv["needs_human"] and conv["needs_human_reason"] == "llm_failure: APITimeoutError"
    assert (await _turns())[0]["error"] == "APITimeoutError"


@pytest.mark.usefixtures("db_ready")
async def test_long_reply_is_split(graph_api: respx.Route, fake_llm) -> None:
    conv_id = await _seed("tell me everything")
    fake_llm.script = [llm.text("\n\n".join(["p" * 600, "q" * 600]))]
    await process_conversation(conv_id)
    assert sent_texts(graph_api) == ["p" * 600, "q" * 600]


@pytest.mark.usefixtures("db_ready")
async def test_failed_send_does_not_lock_in_handoff(fake_llm) -> None:
    conv_id = await _seed("I want to order")
    fake_llm.script = [llm.tool_use("request_human_help", {"reason": "ready_to_order"}), llm.text("Our team will help!")]
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/messages$").mock(return_value=Response(500, json={"error": {"code": 1}}))
        result = await process_conversation(conv_id)
    assert result.failed
    assert (await _conversation())["needs_human"] is False   # the retry must still reply


def test_admin_resume(client: TestClient) -> None:
    conv_id = run_sql(f"INSERT INTO {TEST_SCHEMA}.conversations (id, channel, external_user_id, needs_human, "
                      f"needs_human_reason) VALUES (gen_random_uuid(), 'whatsapp', '911', TRUE, 'complaint') "
                      f"RETURNING id")[0]["id"]
    path = f"/admin/conversations/{conv_id}/resume"
    assert client.post(path).status_code == 403
    assert client.post(path, headers={"Authorization": "Bearer wrong"}).status_code == 403
    resp = client.post(path, headers={"Authorization": "Bearer test-admin-key"})
    assert resp.status_code == 200 and resp.json()["needs_human"] is False
    assert run_sql(f"SELECT needs_human, needs_human_reason FROM {TEST_SCHEMA}.conversations") == \
        [{"needs_human": False, "needs_human_reason": None}]
    assert client.post(f"/admin/conversations/{uuid.uuid4()}/resume",
                       headers={"Authorization": "Bearer test-admin-key"}).status_code == 404

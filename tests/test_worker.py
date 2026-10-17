"""worker.process_conversation called directly (as a Cloud Tasks retry would). The LLM is the echo fake."""

import uuid

import pytest
import respx
from httpx import Response

from app import repositories as repo
from app.db import get_sessionmaker
from app.webhook_parser import IncomingMessage
from app.worker import MAX_ATTEMPTS, process_conversation
from tests.conftest import MESSAGES_URL, TEST_SCHEMA, _execute, sent_texts

pytestmark = pytest.mark.usefixtures("db_ready")


async def _seed(*texts: str) -> uuid.UUID:
    """One conversation with inbound text messages in 'received' state."""
    async with get_sessionmaker()() as session, session.begin():
        conv_id = None
        for i, body in enumerate(texts):
            msg = IncomingMessage(
                id=f"wamid.in{i}", from_="919876543210", type="text", timestamp="1760700000", text=body,
                profile_name="Asha Rao",
            )
            conv_id = await repo.upsert_conversation(session, "whatsapp", msg)
            await repo.insert_inbound_message(session, conv_id, msg)
    assert conv_id is not None
    return conv_id


async def _inbound_statuses() -> list[tuple[str, int]]:
    rows = await _execute(
        f"SELECT status, attempts FROM {TEST_SCHEMA}.messages WHERE direction = 'in' ORDER BY created_at"
    )
    return [(r["status"], r["attempts"]) for r in rows]


async def test_pending_messages_answered_in_one_run_in_order(graph_api: respx.Route, fake_llm) -> None:
    conv_id = await _seed("first", "second", "third")
    result = await process_conversation(conv_id)
    assert (result.processed, result.failed) == (3, False)
    assert len(fake_llm.calls) == 1
    assert fake_llm.calls[0]["messages"] == [{"role": "user", "content": "first\nsecond\nthird"}]
    assert sent_texts(graph_api) == ["You said: first\nsecond\nthird"]
    assert await _inbound_statuses() == [("processed", 0)] * 3


async def test_retried_task_does_not_double_reply(graph_api: respx.Route, fake_llm) -> None:
    conv_id = await _seed("hello")
    await process_conversation(conv_id)
    again = await process_conversation(conv_id)  # Cloud Tasks redelivery
    assert again.processed == 0
    assert len(fake_llm.calls) == 1
    assert sent_texts(graph_api) == ["You said: hello"]
    outbound = await _execute(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.messages WHERE direction = 'out'")
    assert outbound == [{"n": 1}]


async def test_send_failure_marks_failed_then_retry_succeeds() -> None:
    conv_id = await _seed("first", "second")
    error = {"error": {"message": "(#131000) Something went wrong", "code": 131000}}
    with respx.mock(assert_all_called=False) as mock:
        mock.post(MESSAGES_URL).mock(return_value=Response(500, json=error))
        result = await process_conversation(conv_id)
    assert result.failed and result.processed == 0
    assert await _inbound_statuses() == [("failed", 1), ("failed", 1)]

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(MESSAGES_URL).mock(
            side_effect=lambda req: Response(200, json={"messages": [{"id": f"wamid.ok{uuid.uuid4()}"}]})
        )
        result = await process_conversation(conv_id)
    assert (result.processed, result.failed) == (2, False)
    assert sent_texts(route) == ["You said: first\nsecond"]
    assert await _inbound_statuses() == [("processed", 1), ("processed", 1)]


async def test_gives_up_after_max_attempts(graph_api: respx.Route, fake_llm) -> None:
    conv_id = await _seed("stuck", "next")
    await _execute(
        f"UPDATE {TEST_SCHEMA}.messages SET status = 'failed', attempts = {MAX_ATTEMPTS} "
        f"WHERE external_message_id = 'wamid.in0'"
    )
    result = await process_conversation(conv_id)
    assert result.processed == 1
    assert fake_llm.calls[0]["messages"][-1]["content"].endswith("next")


async def test_unknown_conversation_is_noop(graph_api: respx.Route, fake_llm) -> None:
    result = await process_conversation(uuid.uuid4())
    assert (result.processed, result.failed) == (0, False)
    assert graph_api.call_count == 0 and fake_llm.calls == []

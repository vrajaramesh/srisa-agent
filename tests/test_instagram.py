"""Phase 9 increment A: Instagram DMs - parser (real payload shapes), byte splitting, choices with numbered fallback,
echoes (ours vs staff), 24h guard, and a chat through webhook -> worker -> Graph API (mocked)."""

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app import repositories as repo
from app.agent import orders
from app.channels import instagram
from app.channels.instagram import InstagramAdapter, numbered_text, parse_webhook, split_text_bytes
from app.db import get_sessionmaker
from app.models import Conversation, Message
from app.worker import map_numbered_replies, process_conversation
from tests import fake_llm as llm
from tests import mybill_mock as mm
from tests.conftest import IG_MESSAGES_URL, TEST_SCHEMA, _execute, load_fixture, run_sql, sign

CUSTOMER = "1000000000000001"
SHOP = "17841400000000000"
PROFILE_URL = f"https://graph.instagram.com/v25.0/{CUSTOMER}"


@pytest.fixture(autouse=True)
def _reset() -> None:
    instagram.clear_format_failures()


def _ig_ok(request):
    body = json.loads(request.content)
    if "sender_action" in body:
        return Response(200, json={"recipient_id": CUSTOMER})
    return Response(200, json={"recipient_id": CUSTOMER, "message_id": f"mid.out.{uuid.uuid4().hex}"})


@pytest.fixture
def ig_api(router: respx.MockRouter) -> respx.Route:
    router.get(PROFILE_URL).mock(return_value=Response(200, json={"name": "Asha Rao", "username": "asha.styles", "id": CUSTOMER}))
    return router.post(IG_MESSAGES_URL).mock(side_effect=_ig_ok)


def ig_sent(route: respx.Route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls if b"sender_action" not in c.request.content]


# ── Parser (fixtures from real payloads) ─────────────────────────────────────

def test_parse_text() -> None:
    msg, = parse_webhook(load_fixture("ig_text.json"))
    assert (msg.channel, msg.external_user_id, msg.type, msg.text) == ("instagram", CUSTOMER, "text", "Kurtis available?")
    assert msg.timestamp == "1792388088" and not msg.is_echo   # milliseconds -> seconds


def test_parse_echo_belongs_to_the_customer_conversation() -> None:
    msg, = parse_webhook(load_fixture("ig_echo.json"))
    assert msg.is_echo and msg.external_user_id == CUSTOMER and msg.text == "Cheppu"


def test_parse_shared_reel() -> None:
    msg, = parse_webhook(load_fixture("ig_reel.json"))
    assert msg.type == "share"
    assert msg.text.startswith('[shared a reel: "✨ Elegance in every weave! ✨ Our beautiful Chiffon')
    assert msg.text.endswith("\nhi") and "utm_source" not in msg.text        # the URL line is dropped
    assert msg.attachments[0].type == "ig_reel" and msg.attachments[0].url == "https://www.instagram.com/reel/Ddbi7t_NCD4/"
    assert instagram.media_ids(msg.raw) == ["18353818843220298"]


def test_parse_quick_reply_image_postback_and_ignored_events() -> None:
    quick, = parse_webhook(load_fixture("ig_quick_reply.json"))
    assert (quick.type, quick.tapped_id, quick.text) == ("interactive", "size:M", "M")
    image, = parse_webhook(load_fixture("ig_image.json"))
    assert image.type == "image" and image.text is None and image.attachments[0].url.startswith("https://lookaside")
    postback, = parse_webhook(load_fixture("ig_postback.json"))
    assert (postback.type, postback.tapped_id, postback.text) == ("interactive", "ad:order:KUR-101", "Order this")
    assert parse_webhook(load_fixture("ig_read.json")) == []
    assert parse_webhook(load_fixture("ig_comment.json")) == []
    assert parse_webhook(load_fixture("text_message.json")) == []    # a WhatsApp payload


# ── Byte-based splitting ─────────────────────────────────────────────────────

def test_split_counts_bytes_for_telugu() -> None:
    sentence = "మీ కుర్తా కొలతలు చెప్పండి, మేము మీ సైజుకు కుట్టి పంపిస్తాము. "   # ~3 bytes per character
    text = sentence * 30
    assert len(text) < 2500 and len(text.encode()) > 4000
    chunks = split_text_bytes(text)
    assert len(chunks) >= 5
    assert all(len(c.encode("utf-8")) <= 1000 for c in chunks)
    assert all(c.endswith(".") for c in chunks[:-1])                  # cut at sentence ends
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")  # nothing lost
    assert split_text_bytes("Hello 😊") == ["Hello 😊"]
    one_word = "అ" * 700                                               # 2100 bytes, no spaces
    assert [len(c.encode()) for c in split_text_bytes(one_word)] == [999, 999, 102]


# ── Choices: quick replies, numbered fallback, mapping replies back ──────────

async def test_quick_replies_then_numbered_fallback_is_remembered(router: respx.MockRouter, monkeypatch) -> None:
    calls = []

    def reject_quick_replies(request):
        body = json.loads(request.content)
        calls.append(body)
        if "quick_replies" in body["message"]:
            return Response(400, json={"error": {"code": 100, "message": "Invalid parameter"}})
        return Response(200, json={"message_id": "mid.fallback"})

    router.post(IG_MESSAGES_URL).mock(side_effect=reject_quick_replies)
    now = [1000.0]
    monkeypatch.setattr(instagram, "_clock", lambda: now[0])
    choices = [{"id": "fit:SNUG", "title": "Snug"}, {"id": "fit:REGULAR", "title": "Regular"},
               {"id": "fit:RELAXED", "title": "Relaxed"}]
    adapter = InstagramAdapter()

    sent = await adapter.send_choices(CUSTOMER, "How would you like it to fit?", choices)
    assert sent.ok and sent.data["fallback"] == "numbered"
    assert [c["n"] for c in sent.data["offered"]] == [1, 2, 3]
    assert calls[0]["message"]["quick_replies"][1] == {"content_type": "text", "title": "Regular", "payload": "fit:REGULAR"}
    assert calls[1]["message"]["text"] == ("How would you like it to fit?\n\n1. Snug\n2. Regular\n3. Relaxed\n\n"
                                           "Reply 1, 2 or 3 👆")
    await adapter.send_choices(CUSTOMER, "Again?", choices)
    assert len(calls) == 3 and "quick_replies" not in calls[2]["message"]    # format remembered: no retry
    now[0] += 3601
    await adapter.send_choices(CUSTOMER, "After an hour", choices)
    assert "quick_replies" in calls[3]["message"]                            # tried again after an hour


async def test_quick_replies_carry_descriptions_in_the_text(router: respx.MockRouter) -> None:
    route = router.post(IG_MESSAGES_URL).mock(side_effect=_ig_ok)
    rows = [{"id": "size:S", "title": "S", "description": "Bust 34 · Waist 28"},
            {"id": "size:M", "title": "M", "description": "Bust 36 · Waist 30"}]
    sent = await InstagramAdapter().send_choices(CUSTOMER, "Please choose your size 👇", rows, style="list")
    assert sent.ok and "fallback" not in sent.data
    message = json.loads(route.calls[0].request.content)["message"]
    assert message["text"] == "Please choose your size 👇\n\n• S – Bust 34 · Waist 28\n• M – Bust 36 · Waist 30"
    assert [q["payload"] for q in message["quick_replies"]] == ["size:S", "size:M"]


def test_numbered_reply_maps_back_and_other_text_clears_the_offer() -> None:
    conv = Conversation(id=uuid.uuid4(), channel="instagram", external_user_id=CUSTOMER)
    offer = [{"n": 1, "id": "size:S", "title": "S"}, {"n": 2, "id": "size:M", "title": "M"}]
    conv.last_offered_choices = offer
    msg = Message(type="text", text=" 2 ", reply_id=None)
    map_numbered_replies(conv, [msg])
    assert (msg.reply_id, msg.type, msg.text) == ("size:M", "interactive", "M")
    assert conv.last_offered_choices is None

    conv.last_offered_choices = offer
    other, later = Message(type="text", text="what about blue?"), Message(type="text", text="2")
    map_numbered_replies(conv, [other, later])
    assert other.reply_id is None and later.reply_id is None     # the offer was cleared by the first message
    assert numbered_text("Pick one", [{"id": "a", "title": "Only"}]).endswith("Reply 1 👆")


def test_indian_mobile_normalisation() -> None:
    assert orders.indian_mobile_e164("98765 43210") == "+919876543210"
    assert orders.indian_mobile_e164("+91-98765-43210") == "+919876543210"
    assert orders.indian_mobile_e164("09876543210") == "+919876543210"
    assert orders.indian_mobile_e164("12345") is None
    assert orders.indian_mobile_e164("5876543210") is None       # Indian mobiles start with 6-9


# ── Echoes: ours vs staff ────────────────────────────────────────────────────

async def _ig_conversation(last_message_minutes_ago: int = 1) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        return await repo.upsert_conversation_for(
            session, "instagram", CUSTOMER, "Asha", datetime.now(timezone.utc) - timedelta(minutes=last_message_minutes_ago))


@pytest.mark.usefixtures("db_ready")
async def test_echo_of_our_message_is_ignored_and_staff_reply_takes_over(router: respx.MockRouter, fake_llm) -> None:
    conv_id = await _ig_conversation()
    async with get_sessionmaker()() as session, session.begin():
        repo.add_outbound_message(session, conv_id, "Hi! 😊", "mid.ours", {}, author="bot")
        await repo.record_instagram_echo(session, conv_id, "mid.ours", "Hi! 😊", {})
        await repo.record_instagram_echo(session, conv_id, "mid.staff", "Cheppu", {})
    await process_conversation(conv_id)

    echoes = await _execute(f"SELECT mid, decision FROM {TEST_SCHEMA}.instagram_echoes ORDER BY mid")
    assert echoes == [{"mid": "mid.ours", "decision": "ours"}, {"mid": "mid.staff", "decision": "staff"}]
    conv, = await _execute(f"SELECT needs_human, needs_human_reason FROM {TEST_SCHEMA}.conversations")
    assert conv == {"needs_human": True, "needs_human_reason": "staff_took_over"}
    staff, = await _execute(f"SELECT text, author FROM {TEST_SCHEMA}.messages WHERE external_message_id = 'mid.staff'")
    assert staff == {"text": "Cheppu", "author": "staff"}
    assert fake_llm.calls == []                                   # an echo never triggers a reply


@pytest.mark.usefixtures("db_ready")
async def test_no_reply_outside_24h(router: respx.MockRouter, fake_llm) -> None:
    route = router.post(IG_MESSAGES_URL).mock(side_effect=_ig_ok)
    conv_id = await _ig_conversation(last_message_minutes_ago=25 * 60)
    async with get_sessionmaker()() as session, session.begin():
        await repo.insert_inbound(session, conv_id, "mid.late", "text", "hello?", None, {})
    await process_conversation(conv_id)
    assert route.call_count == 0 and fake_llm.calls == []
    msg, = await _execute(f"SELECT status, last_error FROM {TEST_SCHEMA}.messages")
    assert msg == {"status": "skipped", "last_error": "outside_24h"}


# ── Through the webhook ──────────────────────────────────────────────────────

def post_ig(client: TestClient, message: dict, echo: bool = False) -> None:
    data = copy.deepcopy(load_fixture("ig_text.json"))
    event = data["entry"][0]["messaging"][0]
    event["timestamp"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    event["message"] = {"mid": f"mid.in.{uuid.uuid4().hex}", **message}
    if echo:
        event["sender"], event["recipient"] = {"id": SHOP}, {"id": CUSTOMER}
        event["message"]["is_echo"] = True
    body = json.dumps(data).encode()
    resp = client.post("/webhook/instagram", content=body,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200


def test_instagram_chat_order_start_and_numbered_size(client: TestClient, router: respx.MockRouter,
                                                      ig_api: respx.Route, fake_llm) -> None:
    mybill = mm.MybillMock(router)
    fake_llm.script = [
        llm.text("Hi Asha! 😊 Yes, we have kurtis stitched to your size. Which design do you like?"),
        llm.tool_use("start_order", {"design_code": "KUR-101"}, block_id="t1"),
        llm.tool_use("send_size_options", {}, block_id="t2"),
        llm.text("Lovely! Please pick your size."),
        llm.text("Got it, size M. What is your height in cm?"),
    ]
    post_ig(client, {"text": "Kurtis available?"})
    post_ig(client, {"text": "I want KUR-101"})

    sent = ig_sent(ig_api)
    assert sent[0] == {"recipient": {"id": CUSTOMER},
                       "message": {"text": "Hi Asha! 😊 Yes, we have kurtis stitched to your size. Which design do you like?"}}
    sizes = sent[1]["message"]
    assert sizes["text"].startswith("Please choose your size 👇") and "• M – Bust 36" in sizes["text"]
    assert [q["payload"] for q in sizes["quick_replies"]] == ["size:S", "size:M", "size:L"]
    assert mybill.upserts[-1] == {"instagram_user_id": CUSTOMER, "name": "Asha Rao"}   # name from the profile API
    assert any(b"sender_action" in c.request.content for c in ig_api.calls)            # mark_seen

    post_ig(client, {"text": "2"})                                 # typed instead of tapping
    draft, = run_sql(f"SELECT size_code FROM {TEST_SCHEMA}.order_drafts")
    assert draft["size_code"] == "M"
    assert ig_sent(ig_api)[-1]["message"]["text"] == "Got it, size M. What is your height in cm?"

    conv, = run_sql(f"SELECT channel, customer_name, profile_username, stage FROM {TEST_SCHEMA}.conversations")
    assert conv == {"channel": "instagram", "customer_name": "Asha Rao", "profile_username": "asha.styles",
                    "stage": "sizing"}
    facts = fake_llm.calls[-1]["system"][1]["text"]
    assert "- Channel: instagram" in facts and "(@asha.styles)" in facts and "Instagram DM text" in facts
    # Every send was recorded with its Instagram message id (so its echo is recognised as ours).
    out = run_sql(f"SELECT external_message_id, author FROM {TEST_SCHEMA}.messages WHERE direction = 'out'")
    assert out and all(r["external_message_id"].startswith("mid.out.") and r["author"] == "bot" for r in out)


def test_photos_send_caption_as_separate_text(client: TestClient, router: respx.MockRouter, ig_api: respx.Route,
                                              fake_llm) -> None:
    mm.MybillMock(router)
    fake_llm.script = [llm.tool_use("send_design_photos", {"code": "KUR-101"}), llm.text("Here it is 😊")]
    post_ig(client, {"text": "show KUR-101"})
    sent = ig_sent(ig_api)
    assert sent[0]["message"] == {"attachment": {"type": "image", "payload": {"url": "https://img.test/kur-101.jpg"}}}
    assert sent[1]["message"] == {"text": "Anarkali Kurta – ₹1,899"}
    assert sent[2]["message"] == {"text": "Here it is 😊"}


def test_courier_phone_required_on_instagram(client: TestClient, router: respx.MockRouter, ig_api: respx.Route,
                                             fake_llm) -> None:
    mm.MybillMock(router)
    results = []

    def capture(kwargs):
        results.append(kwargs["messages"][-1]["content"])
        return llm.text("Please share a 10-digit mobile number for the courier.")

    fake_llm.script = [
        llm.response(llm.tool_block("start_order", {"design_code": "KUR-101"}, "t1"),
                     llm.tool_block("save_size", {"size_code": "M", "height_cm": 160, "fit_preference": "REGULAR"}, "t2"),
                     stop_reason="tool_use"),
        llm.response(llm.tool_block("choose_option", {"option_id": mm.NECK_ROUND}, "t3a"),
                     llm.tool_block("choose_option", {"option_id": mm.SLEEVE_HALF}, "t3b"),
                     llm.tool_block("finish_customisation", {}, "t3"), stop_reason="tool_use"),
        llm.tool_use("save_delivery_details", {"name": "Asha", "line1": "12 MG Road", "city": "Hyderabad",
                                               "state": "Telangana", "pincode": "500001"}, block_id="t4"),
        capture,
    ]
    post_ig(client, {"text": "order KUR-101"})
    tool_result = json.dumps(results[-1], default=str)
    assert "10-digit Indian mobile for the courier" in tool_result


def test_staff_reply_from_the_app_stops_the_bot(client: TestClient, router: respx.MockRouter, ig_api: respx.Route,
                                               fake_llm, monkeypatch) -> None:
    from app import main
    monkeypatch.setattr(main, "ECHO_DELAY_SECONDS", 0)
    mm.MybillMock(router)
    post_ig(client, {"text": "Hi"})                   # bot replies (echo-bot default of the fake LLM)
    our_mid = run_sql(f"SELECT external_message_id FROM {TEST_SCHEMA}.messages WHERE direction = 'out'")[0]["external_message_id"]
    data_ours = {"text": "You said: Hi", "mid": our_mid}
    post_ig(client, data_ours, echo=True)             # echo of our own reply: ignored
    assert run_sql(f"SELECT needs_human FROM {TEST_SCHEMA}.conversations")[0]["needs_human"] is False
    post_ig(client, {"text": "Cheppu"}, echo=True)    # typed by staff in the Instagram app
    assert run_sql(f"SELECT needs_human FROM {TEST_SCHEMA}.conversations")[0]["needs_human"] is True
    calls_before = len(fake_llm.calls)
    post_ig(client, {"text": "hello?"})               # the team has the chat now
    assert len(fake_llm.calls) == calls_before

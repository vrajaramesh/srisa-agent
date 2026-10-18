"""Phase 8: Click-to-WhatsApp ad referrals - storing entries, resolving the design, the deterministic first reply,
ad:* taps, FEP window, marketing events and order attribution."""

import copy
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from app.agent import ad_entry, fep, orders, resolution
from app.agent.context import facts_block
from app.main import store_webhook
from app.models import Conversation
from app.mybill_client import MybillClient, static_token_provider
from app.webhook_parser import Referral, parse_webhook
from app.worker import process_conversation
from tests import fake_llm as llm
from tests import mybill_mock as mm
from tests.conftest import MYBILL, TEST_SCHEMA, _execute, load_fixture

AD_ID = "120210000000000001"
LABEL = "Mustard kurti reel – Diwali boost"
CAPTION = "Anarkali Kurta – ₹1,899 · stitched to your size in 7 days 🧵"
PHONE = "919876543210"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    resolution.clear_link_cache()


def payload(fixture: str, *, text: str | None = None, referral: dict | None = None, source_id: str | None = None,
            sent_at: datetime | None = None) -> dict[str, Any]:
    """A fixture with a fresh message id and a current timestamp (FEP needs a reply within 24h)."""
    data = copy.deepcopy(load_fixture(fixture))
    message = data["entry"][0]["changes"][0]["value"]["messages"][0]
    message["id"] = f"wamid.{uuid.uuid4().hex}"
    message["timestamp"] = str(int((sent_at or datetime.now(timezone.utc)).timestamp()))
    if text is not None:
        message["text"]["body"] = text
    if referral is not None:
        message["referral"] = referral
    if source_id is not None:
        message["referral"]["source_id"] = source_id
    return data


def text_payload(text: str, sent_at: datetime | None = None) -> dict[str, Any]:
    data = payload("referral_image.json", text=text, sent_at=sent_at)
    del data["entry"][0]["changes"][0]["value"]["messages"][0]["referral"]
    return data


async def deliver(data: dict[str, Any]) -> uuid.UUID:
    """Webhook storage + one worker run (what the webhook + Cloud Task do)."""
    conversation_id, = await store_webhook(parse_webhook(data))
    await process_conversation(conversation_id)
    return conversation_id


def sent(graph_api: respx.Route) -> list[dict]:
    return [json.loads(c.request.content) for c in graph_api.calls if b'"status"' not in c.request.content]


async def conversation_row() -> dict:
    return (await _execute(
        f"SELECT current_product_code, stage, entry_source_type, entry_source_id, entry_ctwa_clid, entry_headline, "
        f"entry_label, entry_resolution, entry_at, fep_expires_at FROM {TEST_SCHEMA}.conversations"))[0]


def mapped(router: respx.MockRouter) -> mm.MybillMock:
    mock = mm.MybillMock(router)
    mock.marketing_links[("AD", AD_ID)] = {"design_code": "KUR-101", "label": LABEL}
    return mock


# ── First reply ──────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("db_ready")
async def test_mapped_ad_gets_photo_and_buttons_without_llm(router: respx.MockRouter, graph_api: respx.Route,
                                                             fake_llm) -> None:
    mock = mapped(router)
    await deliver(payload("referral_image.json"))

    image, buttons = sent(graph_api)
    assert image["type"] == "image" and image["to"] == PHONE
    assert image["image"]["link"] == "https://img.test/kur-101.jpg"
    assert image["image"]["caption"] == CAPTION          # the ad had a welcome_message: no second greeting
    assert buttons["type"] == "interactive"
    assert [b["reply"]["id"] for b in buttons["interactive"]["action"]["buttons"]] == [
        "ad:order:KUR-101", "ad:photos:KUR-101", "ad:sizes"]
    assert fake_llm.calls == []
    assert await _execute(f"SELECT id FROM {TEST_SCHEMA}.llm_turns") == []

    conv = await conversation_row()
    assert conv["current_product_code"] == "KUR-101" and conv["entry_resolution"] == "ad_link"
    assert conv["entry_label"] == LABEL and conv["entry_source_id"] == AD_ID and conv["entry_source_type"] == "ad"
    assert conv["entry_ctwa_clid"].startswith("ARAkLkA8")
    assert conv["entry_headline"] == "Diwali Collection – Mustard Kurti"
    # FEP: our first reply + 72h
    assert timedelta(hours=71) < conv["fep_expires_at"] - datetime.now(timezone.utc) <= timedelta(hours=72)

    messages = await _execute(f"SELECT direction, type, status FROM {TEST_SCHEMA}.messages ORDER BY created_at")
    assert [(m["direction"], m["type"]) for m in messages] == [("in", "text"), ("out", "image"), ("out", "interactive")]
    assert messages[0]["status"] == "processed"
    entry, = await _execute(f"SELECT source_id, ctwa_clid, welcome_text, raw FROM {TEST_SCHEMA}.conversation_entries")
    assert entry["source_id"] == AD_ID and entry["welcome_text"].startswith("Hi 👋") and entry["raw"]["media_type"] == "image"

    event, = mock.events
    assert event["event_type"] == "conversation_started" and event["source_type"] == "AD"
    assert event["source_id"] == AD_ID and event["ctwa_clid"].startswith("ARAkLkA8") and "occurred_at" in event
    assert mock.resolve_calls == [("AD", AD_ID)]


@pytest.mark.usefixtures("db_ready")
async def test_greeting_added_only_without_welcome_message(router: respx.MockRouter, graph_api: respx.Route) -> None:
    mock = mapped(router)
    await deliver(payload("referral_no_clid.json"))
    image = sent(graph_api)[0]
    assert image["image"]["caption"] == f"Hi Asha! 👋 Thanks for checking out this design.\n{CAPTION}"
    assert "ctwa_clid" not in mock.events[0]


@pytest.mark.usefixtures("db_ready")
async def test_video_ad_with_mapped_link(router: respx.MockRouter, graph_api: respx.Route, fake_llm) -> None:
    mapped(router)
    await deliver(payload("referral_video.json"))
    assert [b["type"] for b in sent(graph_api)] == ["image", "interactive"] and fake_llm.calls == []
    assert (await _execute(f"SELECT media_type FROM {TEST_SCHEMA}.conversation_entries"))[0]["media_type"] == "video"


@pytest.mark.usefixtures("db_ready")
async def test_returning_customer_within_7_days_goes_to_the_model(router: respx.MockRouter, graph_api: respx.Route,
                                                                   fake_llm) -> None:
    mapped(router)
    await deliver(text_payload("hello"))
    fake_llm.script = [llm.text("Lovely, this is our Anarkali Kurta 😊")]
    await deliver(payload("referral_image.json"))
    assert len(fake_llm.calls) == 2
    assert not [b for b in sent(graph_api) if b["type"] == "interactive"]
    # ...but the ad still set the design and the facts mention it
    assert (await conversation_row())["current_product_code"] == "KUR-101"
    facts = fake_llm.calls[1]["system"][1]["text"]
    assert f'She came from an ad: "{LABEL}" (it shows design KUR-101)' in facts


@pytest.mark.usefixtures("db_ready")
async def test_first_message_after_7_days_gets_the_first_reply(router: respx.MockRouter, graph_api: respx.Route,
                                                                fake_llm) -> None:
    mapped(router)
    await deliver(text_payload("hello"))
    await _execute(f"UPDATE {TEST_SCHEMA}.messages SET created_at = created_at - interval '8 days'")
    await deliver(payload("referral_image.json"))
    assert len(fake_llm.calls) == 1   # only the old "hello"
    assert [b["type"] for b in sent(graph_api)][-2:] == ["image", "interactive"]


@pytest.mark.usefixtures("db_ready")
async def test_unmapped_ad_falls_back_to_ai_with_headline(router: respx.MockRouter, graph_api: respx.Route,
                                                          fake_llm) -> None:
    mock = mm.MybillMock(router)   # no marketing links; ad body/headline/text have no design code
    fake_llm.script = [llm.text("Thanks for your interest! Which design did you like?")]
    await deliver(payload("referral_no_clid.json"))
    assert len(fake_llm.calls) == 1
    facts = fake_llm.calls[0]["system"][1]["text"]
    assert ("Customer came from an ad: headline=Diwali Collection – Mustard Kurti; "
            "ask which design they liked and show latest designs.") in facts
    conv = await conversation_row()
    assert conv["entry_resolution"] == "none" and conv["current_product_code"] is None
    assert conv["fep_expires_at"] is not None            # still an ad entry we replied to
    assert len(mock.events) == 1                          # still counted as a conversation from the ad
    assert [b["type"] for b in sent(graph_api)] == ["text"]


@pytest.mark.usefixtures("db_ready")
async def test_ad_text_resolution_and_no_fep_for_posts(router: respx.MockRouter, graph_api: respx.Route,
                                                       fake_llm) -> None:
    mock = mm.MybillMock(router)
    await deliver(payload("referral_post.json"))     # body mentions KUR-101, no link for the post
    assert mock.resolve_calls == [("IG_MEDIA", "17912345678901234")]
    conv = await conversation_row()
    assert conv["entry_resolution"] == "ad_text" and conv["current_product_code"] == "KUR-101"
    assert conv["fep_expires_at"] is None                 # FEP is for Click-to-WhatsApp ads only
    assert fake_llm.calls == [] and [b["type"] for b in sent(graph_api)] == ["image", "interactive"]
    assert mock.events[0]["source_type"] == "IG_MEDIA"


@pytest.mark.usefixtures("db_ready")
async def test_no_fep_when_reply_is_later_than_24h(router: respx.MockRouter, graph_api: respx.Route) -> None:
    mapped(router)
    await deliver(payload("referral_image.json", sent_at=datetime.now(timezone.utc) - timedelta(hours=25)))
    assert (await conversation_row())["fep_expires_at"] is None


@pytest.mark.usefixtures("db_ready")
async def test_second_ad_overwrites_entry_and_keeps_history(router: respx.MockRouter, graph_api: respx.Route) -> None:
    mock = mapped(router)
    mock.marketing_links[("AD", "120210000000000777")] = {"design_code": "KUR-101", "label": "Second ad"}
    await deliver(payload("referral_image.json"))
    await deliver(payload("referral_video.json", source_id="120210000000000777"))
    conv = await conversation_row()
    assert conv["entry_source_id"] == "120210000000000777" and conv["entry_label"] == "Second ad"
    assert conv["entry_ctwa_clid"] == "ARBvGQ3pN0rQe1e8kDkQ_VIDEO_CLICK_ID"
    rows = await _execute(f"SELECT source_id FROM {TEST_SCHEMA}.conversation_entries ORDER BY created_at, id")
    assert [r["source_id"] for r in rows] == [AD_ID, "120210000000000777"]
    assert len(mock.events) == 2


@pytest.mark.usefixtures("db_ready")
async def test_event_reported_once_when_send_is_retried(router: respx.MockRouter, fake_llm) -> None:
    mock = mapped(router)
    graph = router.post("https://graph.facebook.com/v25.0/106540352242922/messages")
    graph.mock(side_effect=[Response(200, json={"success": True}),           # mark as read
                            Response(500, json={"error": {"message": "boom"}}),
                            Response(200, json={"success": True}),           # mark as read (retry)
                            Response(200, json={"messages": [{"id": "wamid.o1"}]}),
                            Response(200, json={"messages": [{"id": "wamid.o2"}]})])
    conversation_id, = await store_webhook(parse_webhook(payload("referral_image.json")))
    assert (await process_conversation(conversation_id)).failed
    assert (await process_conversation(conversation_id)).processed == 1
    assert len(mock.events) == 1
    assert fake_llm.calls == []


# ── ad:* taps ────────────────────────────────────────────────────────────────

def tap_payload(reply_id: str, title: str) -> dict[str, Any]:
    data = text_payload("x")
    message = data["entry"][0]["changes"][0]["value"]["messages"][0]
    del message["text"]
    message.update({"type": "interactive", "interactive": {"type": "button_reply",
                                                            "button_reply": {"id": reply_id, "title": title}}})
    return data


@pytest.mark.usefixtures("db_ready")
async def test_order_tap_starts_the_order_then_model_asks_size(router: respx.MockRouter, graph_api: respx.Route,
                                                               fake_llm) -> None:
    mapped(router)
    await deliver(payload("referral_image.json"))
    fake_llm.script = [llm.text("Lovely choice! Which size do you usually wear?")]
    await deliver(tap_payload("ad:order:KUR-101", "🛍️ Order this"))
    conv = await conversation_row()
    assert conv["stage"] == "sizing"
    draft, = await _execute(f"SELECT design_code, mybill_customer_id FROM {TEST_SCHEMA}.order_drafts")
    assert draft == {"design_code": "KUR-101", "mybill_customer_id": 512}
    assert len(fake_llm.calls) == 1 and "[tapped: 🛍️ Order this]" in json.dumps(fake_llm.calls[0]["messages"],
                                                                               ensure_ascii=False)
    assert sent(graph_api)[-1]["text"]["body"] == "Lovely choice! Which size do you usually wear?"


@pytest.mark.usefixtures("db_ready")
async def test_photos_tap_sends_photos_then_model(router: respx.MockRouter, graph_api: respx.Route, fake_llm) -> None:
    mapped(router)
    await deliver(payload("referral_image.json"))
    before = len(sent(graph_api))
    fake_llm.script = [llm.text("Here you go 😊 Would you like to order it?")]
    await deliver(tap_payload("ad:photos:KUR-101", "📸 More photos"))
    new = sent(graph_api)[before:]
    assert [b["type"] for b in new] == ["image", "text"]
    assert new[0]["image"]["link"] == "https://img.test/kur-101.jpg"


# ── Resolution priority + cache (no database) ────────────────────────────────

@pytest.fixture
async def mybill_http(router: respx.MockRouter):
    async with httpx.AsyncClient() as http:
        yield MybillClient("https://mybill.test", static_token_provider(None), http)


def referral(**kw: Any) -> Referral:
    base = {"source_id": AD_ID, "source_type": "ad", "headline": "Diwali Collection", "body": "Our new kurti"}
    return Referral(**{**base, **kw})


def conv() -> Conversation:
    return Conversation(id=uuid.uuid4(), channel="whatsapp", external_user_id=PHONE, customer_name="Asha",
                        stage="browsing", needs_human=False)


async def test_resolution_priority(router: respx.MockRouter, mybill_http: MybillClient) -> None:
    mock = mm.MybillMock(router)
    mock.marketing_links[("AD", AD_ID)] = {"design_code": "KUR-101", "label": LABEL}

    c = conv()   # 1. ad link wins over codes in the ad text and her message
    r = await resolution.resolve_entry(c, referral(body="Try KUR-999"), ["I like KUR-555"], mybill_http)
    assert (r.method, c.current_product_code, c.entry_label) == ("ad_link", "KUR-101", LABEL)

    mock.marketing_links[("AD", AD_ID)] = {"design_code": "KUR-999", "label": "Old ad"}   # inactive / gone design
    resolution.clear_link_cache()
    c = conv()   # 2. link's design not available -> code in the ad body
    r = await resolution.resolve_entry(c, referral(body="New: kur-101 in mustard"), [], mybill_http)
    assert (r.method, c.current_product_code, c.entry_label) == ("ad_text", "KUR-101", "Old ad")

    mock.marketing_links.clear()
    resolution.clear_link_cache()
    c = conv()   # 2b. headline also counts
    assert (await resolution.resolve_entry(c, referral(headline="KUR-101 is back"), [], mybill_http)).method == "ad_text"

    c = conv()   # 3. her text
    r = await resolution.resolve_entry(c, referral(), ["do you have KUR-101 in M?"], mybill_http)
    assert (r.method, c.current_product_code) == ("message_text", "KUR-101")

    c = conv()   # nothing
    r = await resolution.resolve_entry(c, referral(), ["hi"], mybill_http)
    assert (r.method, c.current_product_code, c.entry_resolution) == ("none", None, "none")


async def test_link_cache(router: respx.MockRouter, mybill_http: MybillClient, monkeypatch) -> None:
    mock = mm.MybillMock(router)
    mock.marketing_links[("AD", AD_ID)] = {"design_code": "KUR-101", "label": LABEL}
    now = [1000.0]
    monkeypatch.setattr(resolution, "_clock", lambda: now[0])

    assert (await resolution.marketing_link(mybill_http, "AD", AD_ID))["design_code"] == "KUR-101"
    now[0] += 599
    await resolution.marketing_link(mybill_http, "AD", AD_ID)
    assert len(mock.resolve_calls) == 1                  # cached for 10 minutes
    now[0] += 2
    await resolution.marketing_link(mybill_http, "AD", AD_ID)
    assert len(mock.resolve_calls) == 2

    assert await resolution.marketing_link(mybill_http, "AD", "999") is None
    now[0] += 59
    assert await resolution.marketing_link(mybill_http, "AD", "999") is None
    assert len(mock.resolve_calls) == 3                  # 404 cached for 1 minute
    mock.marketing_links[("AD", "999")] = {"design_code": "KUR-101", "label": "Now mapped"}
    now[0] += 2
    assert (await resolution.marketing_link(mybill_http, "AD", "999"))["label"] == "Now mapped"

    mock.resolve_unavailable = True                      # mybill down: not cached
    assert await resolution.marketing_link(mybill_http, "AD", "555") is None
    mock.resolve_unavailable = False
    mock.marketing_links[("AD", "555")] = {"design_code": "KUR-101", "label": "x"}
    assert await resolution.marketing_link(mybill_http, "AD", "555") is not None


# ── FEP helper, facts, attribution ───────────────────────────────────────────

def test_fep_window_rules() -> None:
    now = datetime.now(timezone.utc)
    c = conv()
    assert not fep.opens_fep(c, now, now)                          # organic
    c.entry_source_type = "post"
    assert not fep.opens_fep(c, now, now)                          # IG post: no FEP
    c.entry_source_type = "ad"
    assert fep.opens_fep(c, now - timedelta(hours=23), now)
    assert not fep.opens_fep(c, now - timedelta(hours=25), now)    # replied outside the service window
    assert not fep.is_free_template_window(c, now)
    c.fep_expires_at = fep.fep_expiry(now)
    assert not fep.opens_fep(c, now, now)                          # already open
    assert fep.is_free_template_window(c, now + timedelta(hours=71))
    assert not fep.is_free_template_window(c, now + timedelta(hours=73))


def test_facts_without_entry_are_unchanged() -> None:
    assert "came from" not in facts_block(conv()).lower()


def test_order_request_carries_attribution() -> None:
    from app.models import OrderDraft
    draft = OrderDraft(id=uuid.uuid4(), version=3, design_code="KUR-101", size_code="M", option_ids=[], qty=1)
    c = conv()
    assert "source_type" not in orders.order_request(draft, 512, conversation=c)
    c.entry_source_type, c.entry_source_id, c.entry_ctwa_clid = "ad", AD_ID, "ARAkLk"
    body = orders.order_request(draft, 512, conversation=c)
    assert (body["source_type"], body["source_id"], body["ctwa_clid"]) == ("AD", AD_ID, "ARAkLk")
    c.entry_source_type, c.entry_source_id, c.entry_ctwa_clid = "post", "17912345678901234", None
    body = orders.order_request(draft, 512, conversation=c)
    assert (body["source_type"], body["source_id"]) == ("IG_MEDIA", "17912345678901234") and "ctwa_clid" not in body
    c.entry_source_id = "not-numeric"
    assert "source_type" not in orders.order_request(draft, 512, conversation=c)


def test_marketing_event_payload() -> None:
    c = conv()
    c.mybill_customer_id = 512
    at = datetime(2026, 10, 18, 4, 45, tzinfo=timezone.utc)
    assert ad_entry.marketing_event(c, referral(ctwa_clid="clid"), at) == {
        "event_type": "conversation_started", "source_type": "AD", "source_id": AD_ID, "ctwa_clid": "clid",
        "customer_id": 512, "occurred_at": "2026-10-18T04:45:00+00:00"}
    assert ad_entry.marketing_event(c, referral(source_id="abc"), at) is None
    assert ad_entry.marketing_event(c, referral(source_type="unknown"), at) is None

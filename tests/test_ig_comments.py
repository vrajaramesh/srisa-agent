"""Comments on our posts/reels -> private DM with the design + WhatsApp order link, and a short public reply."""

import copy
import json
import uuid
from datetime import datetime, timezone

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app import ig_comments
from app.channels.instagram import parse_comments
from tests import mybill_mock as mm
from tests.conftest import IG_MESSAGES_URL, TEST_SCHEMA, load_fixture, run_sql, sign

CUSTOMER, SHOP, MEDIA = "1000000000000001", "17841400000000000", "18353818843220298"
MEDIA_URL = f"https://graph.instagram.com/v25.0/{MEDIA}"


@pytest.fixture(autouse=True)
def _reset_formats() -> None:
    from app.channels import instagram
    instagram.clear_format_failures()


CAPTION = "✨ Elegance in every weave! ✨ Chiffon tissue with border work.\nCode: KUR-101\nDM us to order 💜"


def test_buying_intent() -> None:
    for text in ("Price?", "price pls", "How much", "I want this", "Available in XL", "DM details", "entha",
                 "rate entha andi", "ఎంత", "ధర ఎంత?", "कितना है", "Kitne ka hai", "KUR-101", "interested"):
        assert ig_comments.has_buying_intent(text), text
    for text in ("Beautiful 😍", "Nice colour", "❤️❤️", "", None):
        assert not ig_comments.has_buying_intent(text), text


def test_caption_code_and_texts() -> None:
    assert ig_comments.caption_code(CAPTION) == "KUR-101"
    assert ig_comments.caption_code("new LEH-7 and KUR-3, Code: kur-101") == "KUR-101"
    assert ig_comments.caption_code("New arrivals LEH-7") == "LEH-7"
    assert ig_comments.caption_code("no code") is None
    assert ig_comments.public_text("asha", "KUR-101") == (
        "@asha Thank you 💜 We've sent you the details in a DM. Or WhatsApp us at +91 94916 47691 with code KUR-101.")
    assert ig_comments.public_text(None, None) == "Thank you 💜 We've sent you the details in a DM."


def test_parse_comment_fixture() -> None:
    comment, = parse_comments(load_fixture("ig_comment.json"))
    assert (comment.comment_id, comment.media_id, comment.from_id, comment.username, comment.text) == (
        "18090358865305540", MEDIA, CUSTOMER, "customer.one", "Hi")
    assert parse_comments(load_fixture("ig_text.json")) == []


def post_comment(client: TestClient, text: str, from_id: str = CUSTOMER, comment_id: str | None = None) -> str:
    data = copy.deepcopy(load_fixture("ig_comment.json"))
    value = data["entry"][0]["changes"][0]["value"]
    value["id"] = comment_id or str(uuid.uuid4().int)[:17]
    value["text"], value["from"]["id"], value["from"]["username"] = text, from_id, "asha.styles"
    body = json.dumps(data).encode()
    assert client.post("/webhook/instagram", content=body,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)}).status_code == 200
    return value["id"]


@pytest.fixture
def ig(router: respx.MockRouter) -> dict[str, respx.Route]:
    mm.MybillMock(router)
    router.get(MEDIA_URL).mock(return_value=Response(200, json={"caption": CAPTION, "permalink": "https://www.instagram.com/reel/x/"}))
    sends = router.post(IG_MESSAGES_URL).mock(
        side_effect=lambda r: Response(200, json={"recipient_id": CUSTOMER, "message_id": f"mid.pr.{uuid.uuid4().hex}"}))
    replies = router.post(url__regex=r"https://graph\.instagram\.com/v25\.0/\d+/replies").mock(
        return_value=Response(200, json={"id": "17999999999999999"}))
    return {"sends": sends, "replies": replies}


def comment_row(comment_id: str) -> dict:
    return run_sql(f"SELECT outcome, design_code, private_reply_mid, public_reply_id, error FROM "
                   f"{TEST_SCHEMA}.instagram_comments WHERE comment_id = '{comment_id}'")[0]


def test_buying_comment_gets_private_dm_with_whatsapp_link_and_public_reply(client: TestClient, ig, fake_llm) -> None:
    cid = post_comment(client, "Price please?")
    dm, = [json.loads(c.request.content) for c in ig["sends"].calls]
    assert dm["recipient"] == {"comment_id": cid}
    template = dm["message"]["attachment"]["payload"]
    text = template["text"]
    assert text.startswith("Hi @asha.styles! 😊 Thanks for your interest in Anarkali Kurta (KUR-101) – ₹1,899, "
                           "stitched to your size in 7 days 🧵")
    ref = run_sql(f"SELECT code, ig_user_id, design_code FROM {TEST_SCHEMA}.ref_codes")[0]
    short = f"https://srisafabrics.com/wa/KUR-101/{ref['code']}"
    assert text.endswith("\n" + short)                                     # for instagram.com in a browser
    assert template["buttons"] == [{"type": "web_url", "url": short, "title": "Order on WhatsApp"}]   # app
    assert ref["ig_user_id"] == CUSTOMER and ref["design_code"] == "KUR-101"

    public = ig["replies"].calls[0].request
    assert f"/{cid}/replies" in str(public.url)
    assert b"WhatsApp+us+at+%2B91+94916+47691+with+code+KUR-101" in public.content
    row = comment_row(cid)
    assert row["outcome"] == "replied" and row["design_code"] == "KUR-101" and row["public_reply_id"]
    out, = run_sql(f"SELECT m.external_message_id, m.author, c.channel, c.external_user_id, c.current_product_code, "
                   f"c.profile_username FROM {TEST_SCHEMA}.messages m JOIN {TEST_SCHEMA}.conversations c "
                   f"ON c.id = m.conversation_id WHERE m.direction = 'out'")
    assert out["external_message_id"] == row["private_reply_mid"] and out["author"] == "bot"
    assert (out["channel"], out["external_user_id"], out["current_product_code"], out["profile_username"]) == (
        "instagram", CUSTOMER, "KUR-101", "asha.styles")
    assert fake_llm.calls == []                       # no AI needed for comments


def test_echo_of_the_private_reply_is_ours(client: TestClient, ig, fake_llm, monkeypatch) -> None:
    from app import main
    monkeypatch.setattr(main, "ECHO_DELAY_SECONDS", 0)
    cid = post_comment(client, "want this")
    mid = comment_row(cid)["private_reply_mid"]
    data = copy.deepcopy(load_fixture("ig_echo.json"))
    data["entry"][0]["messaging"][0]["message"]["mid"] = mid
    body = json.dumps(data).encode()
    client.post("/webhook/instagram", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert run_sql(f"SELECT decision FROM {TEST_SCHEMA}.instagram_echoes")[0]["decision"] == "ours"
    assert run_sql(f"SELECT needs_human FROM {TEST_SCHEMA}.conversations")[0]["needs_human"] is False


def test_skips_own_compliments_repeats_and_duplicates(client: TestClient, ig, fake_llm) -> None:
    assert comment_row(post_comment(client, "Thank you 💜 We've sent you the details", from_id=SHOP))["outcome"] == "skipped_own"
    assert comment_row(post_comment(client, "Beautiful 😍"))["outcome"] == "skipped_no_intent"
    first = post_comment(client, "price?")
    assert comment_row(first)["outcome"] == "replied"
    assert comment_row(post_comment(client, "and size?"))["outcome"] == "skipped_recent"
    post_comment(client, "price?", comment_id=first)                # Meta redelivers the same comment
    assert ig["sends"].call_count == 1 and ig["replies"].call_count == 1


def test_unknown_design_still_sends_whatsapp_link(client: TestClient, router: respx.MockRouter, fake_llm) -> None:
    mm.MybillMock(router)
    router.get(MEDIA_URL).mock(return_value=Response(200, json={"caption": "Festive collection ✨"}))
    # Meta rejects the button in a private reply -> plain text with the link.
    sends = router.post(IG_MESSAGES_URL).mock(side_effect=lambda r: Response(400, json={"error": {"code": 100}})
                                              if b"attachment" in r.content else Response(200, json={"message_id": "mid.pr.1"}))
    router.post(url__regex=r".*/replies").mock(return_value=Response(200, json={"id": "1"}))
    cid = post_comment(client, "price?")
    assert len(sends.calls) == 2
    text = json.loads(sends.calls[1].request.content)["message"]["text"]
    assert "Thanks for your comment." in text and "tell us which design you liked" in text
    assert "\nhttps://srisafabrics.com/wa/IG-" in text
    assert comment_row(cid)["design_code"] is None


def test_failed_private_reply_is_retried(client: TestClient, router: respx.MockRouter, fake_llm) -> None:
    mm.MybillMock(router)
    router.get(MEDIA_URL).mock(return_value=Response(200, json={"caption": CAPTION}))
    router.post(IG_MESSAGES_URL).mock(return_value=Response(400, json={"error": {"code": 10, "message": "boom"}}))
    cid = post_comment(client, "price?")
    assert comment_row(cid)["outcome"] == "failed"
    resp = client.post("/tasks/process-comment", json={"comment_id": cid})
    assert resp.status_code == 500 and resp.json() == {"outcome": "failed"}

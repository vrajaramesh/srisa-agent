from app.webhook_parser import parse_webhook
from tests.conftest import load_fixture


def test_parse_text_message() -> None:
    parsed = parse_webhook(load_fixture("text_message.json"))
    assert parsed.statuses == []
    assert len(parsed.messages) == 1
    msg = parsed.messages[0]
    assert msg.from_ == "919876543210"
    assert msg.type == "text"
    assert msg.text == "Do you have red silk sarees?"
    assert msg.profile_name == "Asha Rao"
    assert msg.phone_number_id == "106540352242922"
    assert msg.id.startswith("wamid.")


def test_parse_image_message_has_no_text() -> None:
    parsed = parse_webhook(load_fixture("image_message.json"))
    assert len(parsed.messages) == 1
    assert parsed.messages[0].type == "image"
    assert parsed.messages[0].text is None


def test_parse_status_only() -> None:
    parsed = parse_webhook(load_fixture("status_only.json"))
    assert parsed.messages == []
    assert len(parsed.statuses) == 1
    assert parsed.statuses[0].status == "delivered"
    assert parsed.statuses[0].recipient_id == "919876543210"


def test_parse_empty_or_malformed_payload() -> None:
    assert parse_webhook({}).messages == []
    assert parse_webhook({"entry": [{"changes": [{"value": None}]}]}).messages == []
    assert parse_webhook({"entry": [{"changes": [{"value": {"messages": [{"type": "text"}]}}]}]}).messages == []



# ── Click-to-WhatsApp referrals ──────────────────────────────────────────────

def test_parse_referral_image_ad() -> None:
    msg = parse_webhook(load_fixture("referral_image.json")).messages[0]
    ref = msg.referral
    assert ref is not None and ref.is_ad
    assert ref.source_id == "120210000000000001" and ref.source_type == "ad"
    assert ref.source_url == "https://fb.me/3cr4Wqqkv"
    assert ref.headline == "Diwali Collection – Mustard Kurti"
    assert ref.body.startswith("Mustard yellow Anarkali")
    assert ref.media_type == "image" and ref.image_url.endswith("mustard.jpg") and ref.video_url is None
    assert ref.ctwa_clid.startswith("ARAkLkA8")
    assert ref.welcome_text == "Hi 👋 Welcome to SRISA Boutique! How can we help you?"
    assert msg.text == "Hi! Can I get more info on this?"
    assert msg.raw["referral"]["source_id"] == "120210000000000001"   # raw JSON kept


def test_parse_referral_video_ad() -> None:
    ref = parse_webhook(load_fixture("referral_video.json")).messages[0].referral
    assert ref.media_type == "video"
    assert ref.video_url.endswith("mustard.mp4") and ref.thumbnail_url.endswith("mustard_thumb.jpg")
    assert ref.image_url is None
    assert ref.ctwa_clid == "ARBvGQ3pN0rQe1e8kDkQ_VIDEO_CLICK_ID"


def test_parse_referral_without_clid_or_welcome() -> None:
    ref = parse_webhook(load_fixture("referral_no_clid.json")).messages[0].referral
    assert ref is not None and ref.is_ad
    assert ref.ctwa_clid is None and ref.welcome_text is None


def test_parse_referral_instagram_post() -> None:
    ref = parse_webhook(load_fixture("referral_post.json")).messages[0].referral
    assert ref.source_type == "post" and not ref.is_ad
    assert ref.source_id == "17912345678901234"


def test_plain_message_has_no_referral() -> None:
    assert parse_webhook(load_fixture("text_message.json")).messages[0].referral is None

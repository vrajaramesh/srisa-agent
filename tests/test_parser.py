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


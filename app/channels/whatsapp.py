"""WhatsApp Cloud API adapter (wraps app.whatsapp_client and app.webhook_parser; payloads unchanged)."""

from typing import Any

from app import whatsapp_client
from app.agent.formatting import split_message
from app.channels.base import ChannelAdapter, Choice, ChoiceStyle, NormalizedInbound, SendResult
from app.webhook_parser import IncomingMessage

_TYPES = {"text": "text", "image": "image", "audio": "audio", "video": "video", "interactive": "interactive",
          "button": "interactive"}


def normalize(msg: IncomingMessage) -> NormalizedInbound:
    return NormalizedInbound(
        channel="whatsapp", external_user_id=msg.from_, external_message_id=msg.id, timestamp=msg.timestamp,
        type=_TYPES.get(msg.type, "other"), text=msg.text, tapped_id=msg.reply_id,
        tapped_title=msg.text if msg.reply_id else None, referral=msg.referral, profile_name=msg.profile_name,
        raw=msg.raw)


class WhatsAppAdapter(ChannelAdapter):
    name = "whatsapp"
    image_captions = True

    def split_text(self, text: str) -> list[str]:
        return split_message(text)

    async def send_text(self, to: str, text: str) -> SendResult:
        return await whatsapp_client.send_text(to, text)

    async def send_image(self, to: str, url: str, caption: str | None = None) -> SendResult:
        return await whatsapp_client.send_image(to, url, caption)

    async def send_choices(self, to: str, body: str, choices: list[Choice], style: ChoiceStyle = "buttons",
                           button_text: str = "Choose", section_title: str | None = None) -> SendResult:
        """Reply buttons (<=3) or a list (<=10 rows)."""
        if style == "buttons" and len(choices) <= whatsapp_client.MAX_BUTTONS:
            buttons = [{"id": c["id"], "title": c["title"]} for c in choices]
            return await whatsapp_client.send_buttons(to, body, buttons)
        rows = [{k: c[k] for k in ("id", "title", "description") if c.get(k)} for c in choices]
        return await whatsapp_client.send_list(to, body, button_text, [{"title": section_title or "Options", "rows": rows}])

    async def send_link_button(self, to: str, body: str, button_text: str, url: str) -> SendResult:
        """Interactive cta_url message."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp", "to": to, "type": "interactive",
            "interactive": {"type": "cta_url", "body": {"text": whatsapp_client.clip(body, whatsapp_client.INTERACTIVE_BODY)},
                            "action": {"name": "cta_url", "parameters": {
                                "display_text": whatsapp_client.clip(button_text, whatsapp_client.BUTTON_TITLE),
                                "url": url}}}}
        return await whatsapp_client._post_messages(payload, action="send_link_button")

    async def mark_seen(self, to: str, message_id: str | None) -> SendResult:
        if not message_id:
            return SendResult(ok=True)
        return await whatsapp_client.mark_as_read(message_id)

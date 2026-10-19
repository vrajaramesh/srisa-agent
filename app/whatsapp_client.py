"""Outbound calls to the WhatsApp Cloud API (Graph API)."""

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class SendResult:
    """Outcome of a Graph API call. Never contains the token."""

    ok: bool
    message_id: str | None = None  # "wamid..." Meta assigned to our outbound message
    status_code: int | None = None
    error: str | None = None  # short "code: message" summary for logs / DB
    data: dict[str, Any] = field(default_factory=dict, repr=False)


async def start_client() -> None:
    """Create the shared AsyncClient (called from the FastAPI lifespan)."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0)


async def close_client() -> None:
    """Close the shared AsyncClient (called from the FastAPI lifespan)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _get_client() -> httpx.AsyncClient:
    """Return the shared client, failing loudly if the lifespan didn't start it."""
    if _client is None:
        raise RuntimeError("WhatsApp HTTP client not started; is the app lifespan running?")
    return _client


async def _post_messages(payload: dict[str, Any], action: str) -> SendResult:
    """POST to /{phone_number_id}/messages. Logs failures and returns them instead of raising."""
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.wa_access_token.get_secret_value()}"}
    try:
        resp = await _get_client().post(settings.messages_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        # Log only the exception type/message - never the request headers.
        logger.error("WhatsApp request failed", extra={"fields": {"action": action, "error": repr(exc)}})
        return SendResult(ok=False, error=repr(exc))

    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {}

    if resp.is_success:
        # Send response: {"messaging_product": "whatsapp", "contacts": [...], "messages": [{"id": "wamid..."}]}
        message_id = ((data.get("messages") or [{}])[0]).get("id")
        return SendResult(ok=True, message_id=message_id, status_code=resp.status_code, data=data)

    # Meta's error body looks like {"error": {"message": ..., "type": ..., "code": 131047, ...}}
    err = data.get("error") or {}
    logger.error(
        "WhatsApp API error",
        extra={
            "fields": {
                "action": action,
                "status_code": resp.status_code,
                "error_code": err.get("code"),
                "error_subcode": err.get("error_subcode"),
                "error_message": err.get("message"),
            }
        },
    )
    return SendResult(
        ok=False, status_code=resp.status_code, error=f"{err.get('code')}: {err.get('message')}", data=data
    )


async def send_text(to: str, body: str) -> SendResult:
    """Send a plain text message.

    Free-form text is only allowed inside the 24h customer-service window, i.e. within
    24 hours of the customer's last message - fine for replies.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    return await _post_messages(payload, action="send_text")


async def send_image(to: str, url: str, caption: str | None = None) -> SendResult:
    """Send an image by public URL (Meta downloads it). Caption max 1024 chars."""
    image: dict[str, Any] = {"link": url}
    if caption:
        image["caption"] = caption[:1024]
    payload = {"messaging_product": "whatsapp", "to": to, "type": "image", "image": image}
    return await _post_messages(payload, action="send_image")


# WhatsApp interactive message limits (Cloud API).
MAX_BUTTONS, BUTTON_TITLE, BUTTON_ID = 3, 20, 256
MAX_LIST_ROWS, ROW_TITLE, ROW_DESCRIPTION, ROW_ID, LIST_BUTTON, SECTION_TITLE = 10, 24, 72, 200, 20, 24
INTERACTIVE_BODY = 1024


def clip(text: str | None, limit: int) -> str:
    """Trim to `limit` characters on a character boundary (Python str = code points, so emoji stay whole)."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_buttons(body: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
    """Interactive "button" payload part, limits enforced: <=3 buttons, title <=20, body <=1024."""
    if len(buttons) > MAX_BUTTONS:
        logger.warning("Too many reply buttons; extra dropped", extra={"fields": {"count": len(buttons)}})
    return {
        "type": "button",
        "body": {"text": clip(body, INTERACTIVE_BODY)},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": b["id"][:BUTTON_ID], "title": clip(b["title"], BUTTON_TITLE)}}
            for b in buttons[:MAX_BUTTONS]
        ]},
    }


def build_list(body: str, button_text: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Interactive "list" payload part: <=10 rows in total, row title <=24, description <=72, button <=20."""
    out_sections, remaining = [], MAX_LIST_ROWS
    for section in sections:
        rows = []
        for row in section.get("rows", [])[:remaining]:
            item = {"id": row["id"][:ROW_ID], "title": clip(row["title"], ROW_TITLE)}
            if row.get("description"):
                item["description"] = clip(row["description"], ROW_DESCRIPTION)
            rows.append(item)
        remaining -= len(rows)
        if rows:
            out_sections.append({"title": clip(section.get("title") or "Options", SECTION_TITLE), "rows": rows})
        if remaining <= 0:
            break
    return {
        "type": "list",
        "body": {"text": clip(body, INTERACTIVE_BODY)},
        "action": {"button": clip(button_text, LIST_BUTTON), "sections": out_sections},
    }


async def send_buttons(to: str, body: str, buttons: list[dict[str, str]]) -> SendResult:
    """Reply buttons (the customer's tap comes back as interactive.button_reply with our id)."""
    payload = {"messaging_product": "whatsapp", "to": to, "type": "interactive",
               "interactive": build_buttons(body, buttons)}
    return await _post_messages(payload, action="send_buttons")


async def send_list(to: str, body: str, button_text: str, sections: list[dict[str, Any]]) -> SendResult:
    """List message (the chosen row comes back as interactive.list_reply with our id)."""
    payload = {"messaging_product": "whatsapp", "to": to, "type": "interactive",
               "interactive": build_list(body, button_text, sections)}
    return await _post_messages(payload, action="send_list")


async def send_template(to: str, name: str, language: str, body_params: list[str]) -> SendResult:
    """An approved message template (allowed outside the 24h window, e.g. staff alerts)."""
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
        "name": name, "language": {"code": language},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": clip(p, 1000) or "-"}
                                                        for p in body_params]}]}}
    return await _post_messages(payload, action="send_template")


async def mark_as_read(message_id: str) -> SendResult:
    """Mark an incoming message as read (customer sees blue ticks)."""
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    return await _post_messages(payload, action="mark_as_read")

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


async def mark_as_read(message_id: str) -> SendResult:
    """Mark an incoming message as read (customer sees blue ticks)."""
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    return await _post_messages(payload, action="mark_as_read")

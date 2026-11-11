"""WhatsApp → Dropship intake: the owner forwards a supplier's post (one text + one photo per colour) to the shop
number. Their messages never reach the AI; each one is handed to mybill (/api/agent/dropship/*), which collects them
into a batch, closes it after a quiet minute (our delayed Cloud Task calls process-batches - works with
min-instances=0), builds the dropship collection and talks back to the owner through /internal/events
("owner.message").

Dropship is SRISA's business, so every call goes to SRISA's firm schema.
"""

import base64
import logging
import re
import time
from typing import Any

from app import mybill_client, whatsapp_client
from app.config import get_agent_settings
from app.webhook_parser import IncomingMessage

logger = logging.getLogger(__name__)

# Owner texts that are commands, not part of a forwarded post.
COMMAND = re.compile(r"(?is)^\s*(done|new supplier\b.*|supplier\s+.+)$")


OWNER_CACHE_SECONDS = 60
_owner_cache: tuple[float, set[str]] = (0.0, set())


async def owner_numbers() -> set[str]:
    """Owner numbers: the ones managed in mybill (Collections → Suppliers → Forwarding numbers, cached 1 min) plus
    OWNER_NUMBERS from the environment. If mybill can't be reached, the last list is kept."""
    global _owner_cache
    from app.config import get_settings
    fetched_at, cached = _owner_cache
    if time.monotonic() - fetched_at > OWNER_CACHE_SECONDS:
        try:
            numbers = await _client().dropship_owner_numbers()
            cached = {"".join(ch for ch in n if ch.isdigit()) for n in numbers}
            _owner_cache = (time.monotonic(), cached)
        except Exception as exc:  # noqa: BLE001 - routing must never fail the webhook
            logger.warning("Owner numbers not refreshed", extra={"fields": {"error": repr(exc)[:200]}})
            _owner_cache = (time.monotonic() - OWNER_CACHE_SECONDS + 10, cached)   # retry in 10 s
    return cached | get_settings().owner_number_set


def cached_owner_numbers() -> set[str]:
    """The last known owner numbers without a network call (for reports / follow-ups)."""
    from app.config import get_settings
    return _owner_cache[1] | get_settings().owner_number_set


def payload(msg: IncomingMessage) -> dict[str, Any]:
    """What the task needs from one webhook message (the photo is downloaded in the task, not in the webhook)."""
    raw = msg.raw or {}
    image = raw.get("image") or {}
    return {"wa_message_id": msg.id, "from": msg.from_, "type": msg.type, "text": msg.text,
            "caption": image.get("caption"), "media_id": image.get("id"), "mime_type": image.get("mime_type"),
            "timestamp": msg.timestamp, "reply_id": msg.reply_id}


def _client() -> Any:
    base = mybill_client.get_client()
    if base is None:
        raise RuntimeError("mybill is not configured")
    return base.for_tenant(get_agent_settings().srisa_tenant_schema)


async def handle(p: dict[str, Any]) -> dict[str, Any]:
    """One owner message. Raises on a temporary failure (the Cloud Task retries; mybill ignores duplicates)."""
    mybill = _client()
    text = p.get("text") or ""
    if p.get("reply_id") or (p.get("type") == "text" and COMMAND.match(text)):
        result = await mybill.dropship_owner_reply({"from": p["from"], "reply_id": p.get("reply_id"), "text": text})
        logger.info("DROPSHIP_OWNER_REPLY", extra={"fields": {"result": result}})
        return result
    if p.get("type") == "text":
        return await mybill.dropship_intake({"wa_message_id": p["wa_message_id"], "from": p["from"], "type": "text",
                                             "text": text, "timestamp": p.get("timestamp")})
    if p.get("type") == "image" and p.get("media_id"):
        media = await whatsapp_client.download_media(p["media_id"])
        if media is None:
            raise RuntimeError(f"photo {p['wa_message_id']} could not be downloaded from WhatsApp")
        content, mime = media
        return await mybill.dropship_intake({
            "wa_message_id": p["wa_message_id"], "from": p["from"], "type": "image", "text": p.get("caption"),
            "media_id": p["media_id"], "mime_type": mime, "timestamp": p.get("timestamp"),
            "image_base64": base64.b64encode(content).decode()})
    logger.info("Owner message of type %s ignored by dropship intake", p.get("type"))
    return {"status": "ignored", "type": p.get("type")}


async def extract(batch_id: int) -> dict[str, Any]:
    return await _client().dropship_extract(batch_id)


def needs_extraction(result: dict[str, Any]) -> int | None:
    """The batch to build once the owner chose its supplier (owner-reply result), else None."""
    if result.get("action") in ("supplier_set", "supplier_created") and result.get("batch_id"):
        return int(result["batch_id"])
    return None


async def close_quiet_batches() -> dict[str, Any]:
    return await _client().dropship_process_batches()

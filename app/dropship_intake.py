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
COMMAND = re.compile(r"(?is)^\s*(done|new supplier\b.*|supplier\s+.+|retry\s*#?\d+|price\s+[a-z0-9-]*\s*₹?\s*\d{2,6}(\.\d{1,2})?"
                     r"|(stop\s+)?boost\s+[a-z0-9-]+)\s*$")


OWNER_CACHE_SECONDS = 60
_owner_cache: tuple[float, set[str]] = (float("-inf"), set())   # -inf: fetch on the first message (monotonic starts near 0)


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


_supplier_cache: tuple[float, set[str]] = (float("-inf"), set())


async def supplier_numbers() -> set[str]:
    """Active suppliers' WhatsApp numbers from mybill (cached 1 min): they answer "Is it available?" and are never
    answered by the AI. If mybill can't be reached, the last list is kept."""
    global _supplier_cache
    fetched_at, cached = _supplier_cache
    if time.monotonic() - fetched_at > OWNER_CACHE_SECONDS:
        try:
            numbers = await _client().dropship_supplier_numbers()
            cached = {"".join(ch for ch in n if ch.isdigit()) for n in numbers}
            _supplier_cache = (time.monotonic(), cached)
        except Exception as exc:  # noqa: BLE001 - routing must never fail the webhook
            logger.warning("Supplier numbers not refreshed", extra={"fields": {"error": repr(exc)[:200]}})
            _supplier_cache = (time.monotonic() - OWNER_CACHE_SECONDS + 10, cached)
    return cached


def cached_owner_numbers() -> set[str]:
    """The last known owner numbers without a network call (for reports / follow-ups)."""
    from app.config import get_settings
    return _owner_cache[1] | get_settings().owner_number_set


def payload(msg: IncomingMessage, role: str = "owner") -> dict[str, Any]:
    """What the task needs from one webhook message (the photo is downloaded in the task, not in the webhook).
    role: owner (forwarded posts, commands) or supplier (answers to "Is it available?")."""
    raw = msg.raw or {}
    image = raw.get("image") or {}
    return {"wa_message_id": msg.id, "from": msg.from_, "type": msg.type, "text": msg.text,
            "caption": image.get("caption"), "media_id": image.get("id"), "mime_type": image.get("mime_type"),
            "timestamp": msg.timestamp, "reply_id": msg.reply_id, "role": role}


def _client() -> Any:
    base = mybill_client.get_client()
    if base is None:
        raise RuntimeError("mybill is not configured")
    return base.for_tenant(get_agent_settings().srisa_tenant_schema)


async def handle(p: dict[str, Any]) -> dict[str, Any]:
    """One owner message. Raises on a temporary failure (the Cloud Task retries; mybill ignores duplicates)."""
    mybill = _client()
    text = p.get("text") or ""
    if p.get("role") == "supplier" and not (p.get("reply_id") or "").startswith("avail:"):
        return await mybill.dropship_supplier_message(p["from"], text or p.get("caption"))   # forwarded to the owner
    if (p.get("reply_id") or "").startswith("avail:"):   # Yes / No: is a dropship variant still available?
        from app import dropship_flow
        return await dropship_flow.owner_answer(p["reply_id"], p["from"])
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


async def publish(collection_id: int) -> dict[str, Any]:
    return await _client().dropship_publish(collection_id)


def needs_publishing(result: dict[str, Any]) -> int | None:
    """The collection to post once the owner tapped Approve (owner-reply result), else None."""
    if result.get("action") == "publish" and result.get("collection_id"):
        return int(result["collection_id"])
    return None


async def boost(job: dict[str, Any]) -> dict[str, Any]:
    return await _client().dropship_boost(job)


def needs_boosting(result: dict[str, Any], owner: str) -> dict[str, Any] | None:
    """The boost to create once the owner chose budget, days and audience (owner-reply result), else None."""
    if result.get("action") == "boost" and result.get("collection_id"):
        return {"collection_id": int(result["collection_id"]), "daily_budget": int(result.get("daily_budget") or 200),
                "days": int(result["days"]),
                "audience": result["audience"], "owner": owner}
    return None


async def close_quiet_batches() -> dict[str, Any]:
    return await _client().dropship_process_batches()

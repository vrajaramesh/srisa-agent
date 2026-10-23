"""Product resolution before the LLM.

- resolve_design_code: a design code in the customer's text becomes the current design.
- resolve_entry: for a chat opened from a Click-to-WhatsApp ad / post, first hit wins:
  1. the ad id -> mybill marketing link (ad_link), 2. a design code in the ad's body/headline (ad_text),
  3. a design code in her text (message_text); otherwise "none".
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.models import Conversation
from app.mybill_client import MybillClient, MybillError, MybillNotFound
from app.webhook_parser import Referral

logger = logging.getLogger(__name__)

DESIGN_CODE_RE = re.compile(r"\b([A-Za-z]{3})-(\d+)\b")

AD_LINK, AD_TEXT, MESSAGE_TEXT, NONE = "ad_link", "ad_text", "message_text", "none"

# Meta's referral source_type -> mybill marketing link type.
MYBILL_SOURCE_TYPES = {"ad": "AD", "post": "IG_MEDIA"}

LINK_TTL_SECONDS = 600     # a resolved link is reused for 10 minutes
MISS_TTL_SECONDS = 60      # "no link" (404) for 1 minute, so a newly mapped ad starts working quickly
_link_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}
_clock = time.monotonic    # patched in tests


def find_design_codes(text: str | None) -> list[str]:
    """Design codes in a text, upper-cased, in order of appearance (e.g. 'kur-101' -> 'KUR-101')."""
    return [f"{prefix.upper()}-{number}" for prefix, number in DESIGN_CODE_RE.findall(text or "")]


def mybill_source_type(source_type: str | None) -> str | None:
    return MYBILL_SOURCE_TYPES.get((source_type or "").lower())


def clear_link_cache() -> None:
    _link_cache.clear()


async def marketing_link(mybill: MybillClient, source_type: str, source_id: str) -> dict[str, Any] | None:
    """mybill's {design_code, label} for an ad / post, cached in memory (10 min; misses 1 min).
    mybill being down is not cached and returns None."""
    key = (source_type, source_id)
    cached = _link_cache.get(key)
    if cached and cached[0] > _clock():
        return cached[1]
    try:
        link = await mybill.resolve_marketing_link(source_type, source_id)
    except MybillError as exc:
        logger.warning("Marketing link lookup failed", extra={"fields": {"source_id": source_id, "error": repr(exc)}})
        return None
    _link_cache[key] = (_clock() + (LINK_TTL_SECONDS if link else MISS_TTL_SECONDS), link)
    return link


_COLLECTION_CODE = re.compile(r"\b([A-Z]{2,6}-\d{1,4}[A-Z]?)\b")


def find_collection_codes(text: str | None) -> list[str]:
    """Collection / item codes like MCS-1 or MCS-1B (upper-cased)."""
    return [m.upper() for m in _COLLECTION_CODE.findall((text or "").upper())]


async def _active_collection(mybill: MybillClient, code: str) -> dict[str, Any] | None:
    """An active collection by its code or one of its item codes (None if unknown, inactive or mybill is down)."""
    for path in ("collection", "item"):
        try:
            if path == "collection":
                c = await mybill.get_collection(code)
            else:
                c = await mybill.get_collection((await mybill.get_collection_item(code))["collection"]["code"])
            return c if c.get("active", True) and c.get("items") else None
        except MybillNotFound:
            continue
        except Exception as exc:  # noqa: BLE001 - resolution never raises
            logger.warning("Collection lookup failed", extra={"fields": {"code": code, "error": repr(exc)[:200]}})
            return None
    return None


async def _active_design(mybill: MybillClient, code: str) -> dict[str, Any] | None:
    """The design if it exists and is active (mybill returns 404 for inactive designs)."""
    try:
        return await mybill.get_design(code)
    except MybillError:
        return None


async def resolve_design_code(conversation: Conversation, texts: list[str | None], mybill: MybillClient | None) -> str | None:
    """Set conversation.current_product_code to the LAST code mentioned that exists in mybill.
    Returns the code set, or None. Never raises (a lookup failure just leaves the conversation as is)."""
    if mybill is None:
        return None
    codes = [c for text in texts for c in find_design_codes(text)]
    for code in reversed(codes):
        design = await _active_design(mybill, code)
        if design is None:
            continue
        conversation.current_product_code = design.get("code", code)
        logger.info("Design code resolved", extra={"fields": {"conversation_id": str(conversation.id), "code": code}})
        return conversation.current_product_code
    return None


@dataclass
class EntryResolution:
    method: str                       # ad_link | ad_text | message_text | none
    design: dict[str, Any] | None = None
    label: str | None = None
    collection: dict[str, Any] | None = None   # a fabric / readymade collection instead of a design


async def resolve_entry(conversation: Conversation, referral: Referral, texts: list[str | None],
                        mybill: MybillClient | None) -> EntryResolution:
    """Resolve the design an ad visitor is interested in and record it on the conversation
    (current_product_code, entry_resolution, entry_label). Never raises."""
    result = EntryResolution(NONE)
    if mybill is not None:
        link_type = mybill_source_type(referral.source_type)
        link = await marketing_link(mybill, link_type, referral.source_id) if link_type and referral.source_id else None
        if link:
            result.label = link.get("label")
            design = await _active_design(mybill, link["design_code"]) if link.get("design_code") else None
            if design:
                result.method, result.design = AD_LINK, design
            elif link.get("collection_code") and (collection := await _active_collection(mybill, link["collection_code"])):
                result.method, result.collection = AD_LINK, collection
        if result.design is None and result.collection is None:
            for code in find_design_codes(referral.body) + find_design_codes(referral.headline):
                if design := await _active_design(mybill, code):
                    result.method, result.design = AD_TEXT, design
                    break
        if result.design is None and result.collection is None:
            for code in reversed([c for text in texts for c in find_design_codes(text)]):
                if design := await _active_design(mybill, code):
                    result.method, result.design = MESSAGE_TEXT, design
                    break
        if result.design is None and result.collection is None:
            # "Code: MCS-1" in the ad caption, or a collection / item code she typed
            for method, text_list in ((AD_TEXT, [referral.body, referral.headline]), (MESSAGE_TEXT, texts)):
                for code in [c for text in text_list for c in find_collection_codes(text)]:
                    if collection := await _active_collection(mybill, code):
                        result.method, result.collection = method, collection
                        break
                if result.collection:
                    break
    if result.design is not None:
        conversation.current_product_code = result.design.get("code")
    if result.collection is not None:
        conversation.current_collection_code = result.collection.get("code")
    conversation.entry_resolution = result.method
    conversation.entry_label = result.label
    logger.info("Ad entry resolved", extra={"fields": {
        "conversation_id": str(conversation.id), "source_type": referral.source_type, "source_id": referral.source_id,
        "method": result.method, "code": conversation.current_product_code if result.design else None}})
    return result

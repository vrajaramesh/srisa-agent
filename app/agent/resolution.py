"""Product resolution before the LLM: a design code in the customer's text becomes the current design."""

import logging
import re

from app.models import Conversation
from app.mybill_client import MybillClient, MybillError

logger = logging.getLogger(__name__)

DESIGN_CODE_RE = re.compile(r"\b([A-Za-z]{3})-(\d+)\b")


def find_design_codes(text: str | None) -> list[str]:
    """Design codes in a text, upper-cased, in order of appearance (e.g. 'kur-101' -> 'KUR-101')."""
    return [f"{prefix.upper()}-{number}" for prefix, number in DESIGN_CODE_RE.findall(text or "")]


async def resolve_design_code(conversation: Conversation, texts: list[str | None], mybill: MybillClient | None) -> str | None:
    """Set conversation.current_product_code to the LAST code mentioned that exists in mybill.
    Returns the code set, or None. Never raises (a lookup failure just leaves the conversation as is)."""
    if mybill is None:
        return None
    codes = [c for text in texts for c in find_design_codes(text)]
    for code in reversed(codes):
        try:
            design = await mybill.get_design(code)
        except MybillError:
            continue
        conversation.current_product_code = design.get("code", code)
        logger.info("Design code resolved", extra={"fields": {"conversation_id": str(conversation.id), "code": code}})
        return conversation.current_product_code
    return None

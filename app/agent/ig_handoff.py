"""Instagram is for discovery, WhatsApp for ordering.

On Instagram, when she wants to order, the bot sends an "Order on WhatsApp" link (wa.me with a pre-typed message
"Hi SRISA! I'd like to order KUR-101 🛍️ (ref IG-7K3Q9)"). WhatsApp doesn't let a business start a chat, so she taps
Send once; the WhatsApp agent then recognises the ref: it links her Instagram account to her number (mybill merges
the two customers) and starts with that design, like the Click-to-WhatsApp ad flow.
"""

import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from app.models import Message

REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
REF_TTL = timedelta(days=7)
REF_RE = re.compile(r"\b(IG-[A-HJ-KM-NP-Z2-9]{5})\b", re.IGNORECASE)


def new_ref_code() -> str:
    return "IG-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(5))


def ref_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + REF_TTL


def prefilled_text(design_code: str | None, ref: str) -> str:
    from app import firms
    firm = firms.for_code(design_code)
    if firm is not None and firm.wholesale:           # same number, wholesale wording (matches the /wa short link)
        return f"Hi {firm.name}! I'd like wholesale details for {design_code} 🧵 (ref {ref})"
    what = f"order {design_code}" if design_code else "place an order"
    return f"Hi SRISA! I'd like to {what} 🛍️ (ref {ref})"


def whatsapp_link(number: str, text: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(text)}"


def short_link(base: str, design_code: str | None, ref: str) -> str:
    """{base}/KUR-101/IG-7K3QA (or {base}/IG-7K3QA): redirects to WhatsApp with prefilled_text(design_code, ref)."""
    base = base.rstrip("/")
    return f"{base}/{design_code}/{ref}" if design_code else f"{base}/{ref}"


def find_ref(pending: list[Message]) -> tuple[Message, str] | tuple[None, None]:
    """The newest pending customer message carrying a handoff ref (upper-cased)."""
    for message in reversed(pending):
        match = REF_RE.search(message.text or "")
        if match:
            return message, match.group(1).upper()
    return None, None

"""The firms one agent (and one WhatsApp number) serves, and which firm a chat belongs to.

srisa    = SRISA Fabrics, Hyderabad - retail (stitched designs, fabric & readymade collections), Instagram @srisa_fabrics.
srihitha = Srihitha Looms World, Nandyal - wholesale fabrics, Instagram @srihitha_looms_world, collection codes SLW-…

Every mybill call for a chat goes to that firm's schema (X-Agent-Tenant), so products, collections, stock, orders and
payments stay separate. A chat starts as srisa and moves to another firm when it is clearly about that firm:
an ad/post link saved in that firm, a code with that firm's prefix (SLW-1, SLW-1B), or its Instagram account.
"""

import logging
from dataclasses import dataclass

from app.config import get_agent_settings

logger = logging.getLogger(__name__)

DEFAULT = "srisa"


@dataclass(frozen=True)
class Firm:
    key: str
    schema: str            # mybill tenant schema
    name: str
    about: str             # one line for the model and the team
    wholesale: bool
    code_prefixes: tuple[str, ...]   # collection codes that belong to this firm ("SLW" -> SLW-1, SLW-1B)
    ig_account_id: str = ""          # its Instagram professional account ("" = not connected)
    ig_token_secret: str = "IG_ACCESS_TOKEN"   # Secret Manager secret holding that account's token


def _firms() -> dict[str, Firm]:
    from app.config import get_settings
    s = get_agent_settings()
    srisa_ig = getattr(get_settings(), "ig_account_id", "") if _settings_ok() else ""
    prefixes = tuple(p.strip().upper() for p in s.srihitha_code_prefixes.split(",") if p.strip())
    return {
        "srisa": Firm("srisa", s.srisa_tenant_schema, "SRISA Fabrics",
                      "SRISA Fabrics, Hyderabad - retail boutique: stitched designs, fabric by the metre and readymade",
                      False, (), srisa_ig, "IG_ACCESS_TOKEN"),
        "srihitha": Firm("srihitha", s.srihitha_tenant_schema, "Srihitha Looms World",
                         "Srihitha Looms World, Nandyal - our wholesale fabric business (bulk buyers, shops, tailors)",
                         True, prefixes, s.srihitha_ig_account_id, "IG_ACCESS_TOKEN_SRIHITHA"),
    }


def get(key: str | None) -> Firm:
    firms = _firms()
    return firms.get(key or DEFAULT) or firms[DEFAULT]


def all_firms() -> list[Firm]:
    return list(_firms().values())


def for_code(code: str | None) -> Firm | None:
    """The firm a collection/item code belongs to by its prefix (SLW-1B -> srihitha); None = no firm-specific prefix."""
    head = (code or "").strip().upper().split("-", 1)[0]
    for firm in _firms().values():
        if head and head in firm.code_prefixes:
            return firm
    return None


def _settings_ok() -> bool:
    try:
        from app.config import get_settings
        get_settings()
        return True
    except Exception:  # noqa: BLE001 - agent-only contexts (chat_cli) have no channel settings
        return False


def for_ig_account(account_id: str | None) -> Firm:
    """The firm whose Instagram account received a webhook (unknown -> the default firm)."""
    return next((f for f in _firms().values() if account_id and f.ig_account_id == account_id), get(DEFAULT))


def ig_account_ids() -> set[str]:
    return {f.ig_account_id for f in _firms().values() if f.ig_account_id}


def for_schema(schema: str | None) -> Firm | None:
    return next((f for f in _firms().values() if f.schema == schema), None)


def wholesale_code_in(texts: list[str | None]) -> tuple[Firm, str] | None:
    """The first wholesale-prefixed code (e.g. SLW-1, SLW-1B) in an ad / post / her messages, with its firm."""
    import re
    for firm in _firms().values():
        if not firm.code_prefixes:
            continue
        pattern = re.compile(r"(?i)\b(" + "|".join(map(re.escape, firm.code_prefixes)) + r")-?(\d{1,9}[A-Za-z]?)\b")
        for text in texts:
            if m := pattern.search(text or ""):
                return firm, f"{m.group(1).upper()}-{m.group(2).upper()}"
    return None


def detect(conversation, texts: list[str | None]) -> bool:
    """Move the chat to the wholesale firm when an ad / post / message carries its code. True if it moved."""
    found = wholesale_code_in(texts)
    if found is None:
        return False
    firm, code = found
    moved = switch(conversation, firm, f"code {code}")
    conversation.current_collection_code = code   # the wholesale code she is asking about (lookup_wholesale_code)
    return moved


def switch(conversation, firm: Firm, why: str) -> bool:
    """Move the chat to `firm`. What she was looking at in the other firm no longer applies. True if it changed."""
    if (conversation.firm or DEFAULT) == firm.key:
        return False
    logger.info("Chat moved to another firm", extra={"fields": {
        "conversation_id": str(conversation.id), "from": conversation.firm, "to": firm.key, "why": why}})
    conversation.firm = firm.key
    conversation.current_product_code = None
    conversation.current_collection_code = None
    return True


def code_firm(conversation, code: str | None) -> Firm:
    """The firm to look `code` up in: its prefix firm, else (an unprefixed code) the retail default when the chat
    is with a prefixed firm, else the chat's firm."""
    by_prefix = for_code(code)
    if by_prefix is not None:
        return by_prefix
    current = get(conversation.firm)
    return get(DEFAULT) if current.code_prefixes else current

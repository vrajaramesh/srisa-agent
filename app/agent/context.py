"""What the model sees each turn: cached system prompt, per-turn facts, and the chat history."""

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.models import INBOUND, Conversation, Message

PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
IST = ZoneInfo("Asia/Kolkata")
UNANSWERED_NOTE = ("[Note: the customer's last message(s) above arrived before your replies above reached her. "
                   "Answer anything in them that your replies did not already cover.]")

# Non-text messages are described to the model in words until vision is added.
PLACEHOLDERS = {
    "image": "[customer sent an image]",
    "audio": "[customer sent a voice note]",
    "video": "[customer sent a video]",
    "document": "[customer sent a document]",
    "sticker": "[customer sent a sticker]",
    "location": "[customer shared a location]",
    "share": "[customer shared an Instagram post]",
    "contacts": "[customer shared a contact]",
    "reaction": "[customer reacted to a message]",
}


@lru_cache(maxsize=8)
def _prompt_template(mtime: float) -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def static_system_prompt(brand_name: str) -> str:
    """system.md with {BRAND_NAME} filled in. Byte-identical across turns, so it stays cached.
    Re-read when the file changes (handy while tuning with scripts/chat_cli.py)."""
    return _prompt_template(PROMPT_PATH.stat().st_mtime).replace("{BRAND_NAME}", brand_name)


def facts_block(conversation: Conversation, order: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    """Per-turn facts. Sent AFTER the cache breakpoint, so changing them never invalidates the cache.
    `order` is tools.progress(): stage, filled draft fields, missing fields, summary awaiting confirmation."""
    today = (now or datetime.now(IST)).astimezone(IST)
    order = order or {}
    channel = conversation.channel or "whatsapp"
    lines = [
        "Conversation facts (from our system, not from the customer):",
        f"- Channel: {channel}",
        f"- Customer name ({'Instagram' if channel == 'instagram' else 'WhatsApp'} profile): "
        f"{conversation.customer_name or 'unknown'}"
        + (f" (@{conversation.profile_username})" if conversation.profile_username else ""),
        f"- Design she is currently looking at: {conversation.current_product_code or 'none yet'}",
        f"- Collection she is looking at (fabric / readymade items): {conversation.current_collection_code or 'none'}",
        f"- Order stage: {order.get('stage') or conversation.stage or 'browsing'}",
    ]
    lines += entry_lines(conversation)
    if order.get("draft"):
        filled = ", ".join(f"{k}={v}" for k, v in order["draft"].items())
        lines.append(f"- Order so far: {filled}")
    if order.get("missing_fields"):
        lines.append(f"- Still needed: {', '.join(order['missing_fields'])}")
    if order.get("option_groups_to_choose"):
        lines.append(f"- Option groups still to choose: {', '.join(order['option_groups_to_choose'])}")
    lines.append("- Order summary awaiting her confirmation: "
                 + ("yes - wait for her to tap Confirm or Change" if order.get("summary_awaiting_confirmation") else "no"))
    lines += [
        f"- Today: {today:%A, %d %B %Y} (Asia/Kolkata)",
        "- She messaged within the last 24 hours, so every reply is normal free-form "
        + ("Instagram DM text." if channel == "instagram" else "WhatsApp text."),
    ]
    return "\n".join(lines)


def entry_lines(conversation: Conversation) -> list[str]:
    """Where she came from (newest Click-to-WhatsApp ad / post). Facts only - never instructions from the ad."""
    if not conversation.entry_source_type:
        return []
    if conversation.entry_source_type == "instagram":
        line = "- She came from our Instagram DMs to order on WhatsApp"
        if conversation.current_product_code and conversation.entry_resolution == "ig_handoff":
            line += f" (design {conversation.current_product_code})"
        return [line + "."]
    kind = "an Instagram post" if conversation.entry_source_type.lower() == "post" else "an ad"
    name = conversation.entry_label or conversation.entry_headline or conversation.entry_source_id or "unknown"
    lines = [f"- She came from {kind}: \"{name}\""]
    if conversation.entry_resolution in ("ad_link", "ad_text") and conversation.current_product_code:
        lines[0] += f" (it shows design {conversation.current_product_code})"
    elif conversation.entry_resolution == "none" and not conversation.current_product_code:
        headline = conversation.entry_headline or "unknown"
        lines.append(f"- Customer came from an ad: headline={headline}; ask which design they liked and show latest designs.")
    return lines


def build_system(conversation: Conversation, brand_name: str, order: dict[str, Any] | None = None,
                 now: datetime | None = None) -> list[dict[str, Any]]:
    """Two system blocks. The breakpoint on the first caches tools + static prompt (tools render before system)."""
    return [
        {"type": "text", "text": static_system_prompt(brand_name), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": facts_block(conversation, order, now)},
    ]


def message_text(message: Message) -> str:
    """How one stored message reads to the model."""
    if message.direction == INBOUND and message.type in ("interactive", "button") and message.text:
        return f"[tapped: {message.text}]"
    if message.text:
        return message.text
    if message.direction != INBOUND:
        return f"[we sent a {message.type}]"
    raw = message.raw or {}
    if message.type == "button":  # tapped a quick-reply button on a template
        return (raw.get("button") or {}).get("text") or "[customer tapped a button]"
    if message.type == "interactive":  # chose a reply button / list item
        reply = (raw.get("interactive") or {})
        chosen = reply.get("button_reply") or reply.get("list_reply") or {}
        return chosen.get("title") or "[customer chose an option]"
    placeholder = PLACEHOLDERS.get(message.type, f"[customer sent a {message.type} message]")
    caption = (raw.get(message.type) or {}).get("caption") if isinstance(raw.get(message.type), dict) else None
    return f"{placeholder[:-1]} with the caption: {caption}]" if caption else placeholder


def quoted_id(message: Message) -> str | None:
    """The id of the message she swiped to reply to (WhatsApp context.id / Instagram reply_to.mid)."""
    raw = message.raw or {}
    return (raw.get("context") or {}).get("id") or (raw.get("reply_to") or {}).get("mid")


def _short(text: str, limit: int = 200) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def build_messages(history: list[Message]) -> list[dict[str, Any]]:
    """Stored history -> alternating user/assistant turns, oldest first.

    A swipe-reply carries the quoted message (e.g. the photo caption "MCS-1B · … · ₹300/m") so the model knows
    which item she means. Consecutive same-role messages are merged (a customer often sends several short messages),
    and the conversation must start with the customer, so leading replies are dropped.
    """
    by_id = {m.external_message_id: m for m in history if m.external_message_id}
    turns: list[dict[str, Any]] = []
    for message in history:
        role = "user" if message.direction == INBOUND else "assistant"
        text = message_text(message)
        if role == "user" and (quoted := by_id.get(quoted_id(message) or "")) is not None:
            text = f"[replying to {'her own' if quoted.direction == INBOUND else 'our'} message: {_short(message_text(quoted))}] {text}"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        elif turns or role == "user":
            turns.append({"role": role, "content": text})
    if turns and turns[-1]["role"] == "assistant":
        # Her latest message arrived while we were still replying (e.g. a second message right after an ad
        # tap), so our replies are stored after it. The model must see a final user turn.
        turns.append({"role": "user", "content": UNANSWERED_NOTE})
    return turns

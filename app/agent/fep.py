"""Free entry point (FEP) window for Click-to-WhatsApp ad conversations.

Per Meta's pricing: when a customer messages from a Click-to-WhatsApp ad and the business replies within the
24-hour customer service window, a 72-hour window opens (from that reply) in which all messages, including
templates, are free. fep_expires_at = our first reply + 72h; only ever set for "ad" entries.
"""

from datetime import datetime, timedelta, timezone

from app.models import Conversation

FEP_HOURS = 72
SERVICE_WINDOW = timedelta(hours=24)


def fep_expiry(first_reply_at: datetime) -> datetime:
    return first_reply_at + timedelta(hours=FEP_HOURS)


def opens_fep(conversation: Conversation, customer_message_at: datetime | None, reply_at: datetime) -> bool:
    """True if replying now opens the FEP window: an ad entry, no window yet, and within 24h of her message."""
    if (conversation.entry_source_type or "").lower() != "ad" or conversation.fep_expires_at is not None:
        return False
    return customer_message_at is None or reply_at - customer_message_at <= SERVICE_WINDOW


def is_free_template_window(conversation: Conversation, now: datetime | None = None) -> bool:
    """True while templates to this customer are free (for later reminders)."""
    expires = conversation.fep_expires_at
    return expires is not None and (now or datetime.now(timezone.utc)) < expires

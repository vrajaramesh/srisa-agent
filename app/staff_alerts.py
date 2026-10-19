"""Tell the team when a chat is handed to them (AI asked for help, LLM failure, order problem).

WhatsApp template to STAFF_ALERT_NUMBERS - a template because the shop can't message staff first with free text.
Template "staff_alert" (Utility), body with 4 parameters:
    ⚠️ Customer needs help: {{1}} ({{2}}). Reason: {{3}}. Open the chat: {{4}}
"""

import logging

from app import whatsapp_client
from app.config import get_settings
from app.models import Conversation

logger = logging.getLogger(__name__)

# Handovers staff already know about (they caused them) - no alert.
SILENT_REASONS = ("staff_took_over", "staff_reply", "with_team")


def should_alert(was_needs_human: bool, conversation: Conversation) -> bool:
    reason = conversation.needs_human_reason or ""
    return (not was_needs_human and bool(conversation.needs_human)
            and not reason.startswith(SILENT_REASONS))


def describe(conversation: Conversation) -> tuple[str, str]:
    name = conversation.customer_name or conversation.profile_username or "Customer"
    if conversation.channel == "instagram":
        contact = f"Instagram @{conversation.profile_username}" if conversation.profile_username else "Instagram"
    else:
        contact = "+" + conversation.external_user_id
    return name, contact


async def alert_staff(conversation: Conversation) -> int:
    """Send the alert to every configured number; returns how many were sent. Never raises."""
    settings = get_settings()
    numbers = settings.staff_alert_number_list
    name, contact = describe(conversation)
    reason = (conversation.needs_human_reason or "needs help").replace("_", " ")
    link = (f"{settings.mybill_ui_url.rstrip('/')}/?chat={conversation.id}" if settings.mybill_ui_url
            else "mybill-ui > Customer chats")
    logger.warning("STAFF_NEEDED", extra={"fields": {"conversation_id": str(conversation.id), "reason": reason,
                                                     "channel": conversation.channel, "alert_numbers": len(numbers)}})
    sent = 0
    for number in numbers:
        try:
            result = await whatsapp_client.send_template(number, settings.staff_alert_template,
                                                         settings.staff_alert_template_lang, [name, contact, reason, link])
        except Exception as exc:  # noqa: BLE001
            logger.error("Staff alert failed", extra={"fields": {"error": repr(exc)[:200]}})
            continue
        if result.ok:
            sent += 1
        else:
            logger.error("Staff alert rejected", extra={"fields": {"error": result.error}})
    return sent

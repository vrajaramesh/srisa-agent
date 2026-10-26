"""Tell the team when a chat is handed to them (AI asked for help, LLM failure, order problem).

WhatsApp template to STAFF_ALERT_NUMBERS - a template because the shop can't message staff first with free text.
Template "staff_alert" (Utility, id 4408065276072701), used for every staff alert (handovers, measurement calls):
    🔔 SRISA staff alert / Customer: {{1}} / Contact: {{2}} / Action needed: {{3}} / ...update the order in mybill: {{4}}
"""

import logging

from app import firms, whatsapp_client
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


def chat_link(conversation: Conversation) -> str:
    settings = get_settings()
    return (f"{settings.mybill_ui_url.rstrip('/')}/?chat={conversation.id}" if settings.mybill_ui_url
            else "mybill-ui > Boutique > Customer chats")


async def alert_staff(conversation: Conversation) -> int:
    """A chat was handed to the team. Returns how many alerts were sent. Never raises."""
    name, contact = describe(conversation)
    reason = _tag(firms.get(conversation.firm)) + "needs help – " + (conversation.needs_human_reason or "unspecified").replace("_", " ")
    link = chat_link(conversation)
    logger.warning("STAFF_NEEDED", extra={"fields": {"conversation_id": str(conversation.id), "reason": reason,
                                                     "channel": conversation.channel}})
    return await send_alert(name, contact, reason, link)


async def alert_measurement_call(order_no: str, name: str | None, phone: str | None, design: str | None,
                                 amount: float | None) -> int:
    """A paid order needs the tailor to call the customer for exact measurements."""
    settings = get_settings()
    paid = f", paid ₹{amount:,.0f}" if amount is not None else ""
    reason = f"📏 call for exact measurements – order {order_no}" + (f" ({design})" if design else "") + paid
    link = (f"{settings.mybill_ui_url.rstrip('/')}/?order={order_no}" if settings.mybill_ui_url
            else "mybill-ui > Boutique > WhatsApp Orders")
    logger.warning("MEASUREMENT_CALL_NEEDED", extra={"fields": {"order_no": order_no}})
    return await send_alert(name or "Customer", phone or "-", reason, link)


async def alert_website_order(order_no: str, name: str | None, phone: str | None, amount: float | None,
                              items: str | None, address: str | None) -> int:
    """A website (srisafabrics.com) order was paid: pack and dispatch it."""
    settings = get_settings()
    paid = f" – paid ₹{amount:,.0f}" if amount is not None else ""
    reason = (f"🛍️ pack & dispatch website order {order_no}{paid}" + (f"\nItems: {items}" if items else "")
              + (f"\nShip to: {address}" if address else ""))
    link = (f"{settings.mybill_ui_url.rstrip('/')}/?view=online-orders" if settings.mybill_ui_url
            else "mybill-ui > Online Orders")
    logger.warning("WEBSITE_ORDER_PAID", extra={"fields": {"order_no": order_no}})
    return await send_alert(name or "Customer", phone or "-", reason, link)


async def alert_dispatch(order_no: str, name: str | None, phone: str | None, amount: float | None,
                         items: str | None, address: str | None, firm: "firms.Firm | None" = None) -> int:
    """A paid WhatsApp goods order (fabric / readymade): cut or pack it and dispatch."""
    settings = get_settings()
    paid = f" – paid ₹{amount:,.0f}" if amount is not None else ""
    reason = (f"{_tag(firm)}📦 pack & dispatch WhatsApp order {order_no}{paid}" + (f"\nItems: {items}" if items else "")
              + (f"\nShip to: {address}" if address else ""))
    link = (f"{settings.mybill_ui_url.rstrip('/')}/?order={order_no}" if settings.mybill_ui_url
            else "mybill-ui > Boutique > WhatsApp Orders")
    logger.warning("DISPATCH_NEEDED", extra={"fields": {"order_no": order_no}})
    return await send_alert(name or "Customer", phone or "-", reason, link)


def _tag(firm: "firms.Firm | None") -> str:
    """Wholesale (Srihitha) alerts say so first: the team handles them in that firm's mybill login."""
    return f"[{firm.name} – wholesale] " if firm is not None and firm.wholesale else ""


def alert_text(name: str, contact: str, reason: str, link: str) -> str:
    return (f"🔔 SRISA staff alert\n\nCustomer: {name}\nContact: {contact}\nAction needed: {reason}\n\n"
            f"Please follow up with the customer and update the order in mybill: {link}")


async def send_alert(name: str, contact: str, reason: str, link: str) -> int:
    """To every STAFF_ALERT_NUMBERS: a normal text first (free, delivered at once if that staff phone messaged the
    shop number in the last 24 h), else the approved "staff_alert" template. Returns how many were delivered."""
    settings = get_settings()
    sent = 0
    for number in settings.staff_alert_number_list:
        try:
            result = await whatsapp_client.send_text(number, alert_text(name, contact, reason, link))
            how = "text"
            if not result.ok:
                result = await whatsapp_client.send_template(number, settings.staff_alert_template,
                                                             settings.staff_alert_template_lang,
                                                             [name, contact, reason, link])
                how = "template"
        except Exception as exc:  # noqa: BLE001
            logger.error("STAFF_ALERT_UNDELIVERED", extra={"fields": {"error": repr(exc)[:200]}})
            continue
        if result.ok:
            sent += 1
            logger.info("Staff alert sent", extra={"fields": {"via": how}})
        else:
            # Neither: the staff phone hasn't messaged the shop in 24 h and the template isn't approved yet.
            logger.error("STAFF_ALERT_UNDELIVERED", extra={"fields": {"error": result.error,
                         "hint": "staff phone should message the shop number, or wait for template approval"}})
    return sent

"""Payment events from mybill-service (POST /internal/events) -> a WhatsApp message to the customer.

mybill owns payments; it calls us after a Razorpay webhook (order.paid, payment_link.expired/cancelled).
Deduped by event_id in agent.processed_events, recorded in the same transaction as the message, so a retry
after a failed send re-sends and a duplicate after success is ignored.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import firms, staff_alerts, whatsapp_client
from app import repositories as repo
from app.config import get_settings
from app.agent.formatting import format_rupees
from app.db import get_sessionmaker
from app.models import Conversation, ProcessedEvent
from app.worker import WorkerMessenger

logger = logging.getLogger(__name__)


class PaymentEvent(BaseModel):
    event: str
    order_no: str
    customer_phone: str | None = None
    customer_name: str | None = None
    amount: float | None = None
    estimated_delivery_date: str | None = None
    event_id: str
    exact_measurements_required: bool = False   # the tailor calls her for exact measurements (design flag)
    design_code: str | None = None
    design_name: str | None = None
    order_type: str | None = None   # GARMENT or FABRIC
    # Website (srisafabrics.com) events: "ecom.order.paid" (alert staff), "ecom.reset_code" (send code).
    items: str | None = None
    delivery_address: str | None = None
    code: str | None = None
    tenant: str | None = None   # mybill firm schema of the order (firm_srisa, firm_srihitha)
    # Courier events (order.dispatched / order.out_for_delivery / order.delivered)
    courier: str | None = None
    awb: str | None = None
    tracking_url: str | None = None
    courier_location: str | None = None
    # "owner.message" (WhatsApp → Dropship intake): text or a list message to the owner (customer_phone)
    kind: str | None = None                       # text | list
    text: str | None = None
    button_text: str | None = None
    sections: list[dict[str, Any]] | None = None
    image_url: str | None = None                  # sent just before the message (a list can't have an image header)
    buttons: list[dict[str, Any]] | None = None   # kind "buttons": up to 3 reply buttons [{id, title}]
    supplier_name: str | None = None              # DROPSHIP order.paid: who ships it
    supplier_phone: str | None = None
    owner_phone: str | None = None                # DROPSHIP order.paid: the owner who forwarded the post


def _first_name(name: str | None) -> str:
    return (name or "").split(" ")[0].strip()


def _date(value: str | None) -> str | None:
    try:
        return date.fromisoformat(value).strftime("%d %b") if value else None
    except ValueError:
        return value


def message_for(event: PaymentEvent) -> str | None:
    """Customer-facing text for an event (None = nothing to say)."""
    if event.event == "order.paid":
        thanks = f"Thank you, {_first_name(event.customer_name)}! " if _first_name(event.customer_name) else "Thank you! "
        amount = f" ({format_rupees(event.amount)})" if event.amount is not None else ""
        eta = _date(event.estimated_delivery_date)
        status = ("Your fabric is being cut and packed 📦" if event.order_type == "FABRIC"
                  else "Your order is being packed for dispatch 📦" if event.order_type in GOODS_TYPES
                  else "Your outfit is now with our tailor 🧵")
        text = (f"✅ Payment received for order {event.order_no}{amount}. {thanks}"
                f"{status}" + (f" - estimated delivery {eta}." if eta else "."))
        if event.exact_measurements_required and event.order_type not in GOODS_TYPES:
            phone = f" on {event.customer_phone}" if event.customer_phone else ""
            text += f"\n📏 Our tailor will call you{phone} shortly to take your exact measurements."
        return text
    if event.event in COURIER_EVENTS:
        return courier_text(event)
    if event.event == "payment_link.expired":
        return f"⏰ The payment link for order {event.order_no} has expired. Reply \"pay\" and I'll send you a new one."
    if event.event == "payment_link.cancelled":
        return f"The payment link for order {event.order_no} was cancelled. Reply \"pay\" if you'd like a new link."
    return None


WEBSITE_ORDER_PAID, RESET_CODE = "ecom.order.paid", "ecom.reset_code"
DISPATCHED, OUT_FOR_DELIVERY, DELIVERED = "order.dispatched", "order.out_for_delivery", "order.delivered"
COURIER_EVENTS = (DISPATCHED, OUT_FOR_DELIVERY, DELIVERED)
REPLY_WINDOW = timedelta(hours=24)


def _courier_name(event: "PaymentEvent") -> str:
    return (event.courier or "courier").title()


def courier_status(event: "PaymentEvent") -> str:
    """The middle of the update: "has been shipped with Delhivery (tracking no. 123...)"."""
    if event.event == DISPATCHED:
        return f"has been shipped with {_courier_name(event)} (tracking no. {event.awb})"
    if event.event == OUT_FOR_DELIVERY:
        return "is out for delivery today"
    return "has been delivered"


def courier_text(event: "PaymentEvent") -> str:
    hi = f"Hi {_first_name(event.customer_name)}! " if _first_name(event.customer_name) else ""
    track = f"\nTrack it here 👉 {event.tracking_url}" if event.tracking_url else ""
    if event.event == DISPATCHED:
        return (f"📦 {hi}Your order {event.order_no} has been shipped with {_courier_name(event)}.\n"
                f"Tracking number (AWB): {event.awb}{track}")
    if event.event == OUT_FOR_DELIVERY:
        return f"🚚 {hi}Your order {event.order_no} is out for delivery today. Please keep your phone handy.{track}"
    return f"✅ {hi}Your order {event.order_no} has been delivered. Thank you for shopping with SRISA! 💜"
GOODS_TYPES = ("FABRIC", "READYMADE", "MIXED", "DROPSHIP")   # ready goods: packed and dispatched (dropship: by the supplier)
WEBSITE_EVENTS = (WEBSITE_ORDER_PAID, RESET_CODE)


async def _first_time(event: PaymentEvent) -> bool:
    async with get_sessionmaker()() as session, session.begin():
        return (await session.execute(
            pg_insert(ProcessedEvent).values(event_id=event.event_id, event_type=event.event, order_no=event.order_no)
            .on_conflict_do_nothing(index_elements=["event_id"]).returning(ProcessedEvent.event_id)
        )).scalar_one_or_none() is not None


async def _handle_website(event: PaymentEvent) -> dict[str, Any]:
    """Website events. A reset code goes to the customer as the AUTHENTICATION template (never logged);
    a paid website order alerts the staff to pack and dispatch it."""
    if event.event == RESET_CODE:
        wa_id = "".join(ch for ch in (event.customer_phone or "") if ch.isdigit())
        if not wa_id or not (event.code or "").isdigit():
            return {"status": "invalid"}
        settings = get_settings()
        result = await whatsapp_client.send_auth_code(wa_id, settings.reset_code_template,
                                                      settings.reset_code_template_lang, event.code)
        if not result.ok:
            raise RuntimeError(f"reset code not sent: {result.error}")
        logger.info("RESET_CODE_SENT", extra={"fields": {"event_id": event.event_id}})
        return {"status": "sent"}
    if not await _first_time(event):
        return {"status": "duplicate"}
    sent = await staff_alerts.alert_website_order(event.order_no, event.customer_name, event.customer_phone,
                                                  event.amount, event.items, event.delivery_address)
    return {"status": "alerted", "staff_alerts_sent": sent}


async def handle(event: PaymentEvent) -> dict[str, Any]:
    """Send the message once. Returns {"status": ...}; raises RuntimeError if WhatsApp failed (-> 500, mybill retries).
    A paid order that needs exact measurements also alerts the staff (once, after the event is recorded)."""
    if event.event in WEBSITE_EVENTS:
        return await _handle_website(event)
    if event.event in COURIER_EVENTS:
        return await _handle_courier(event)
    if event.event == OWNER_MESSAGE:
        return await _handle_owner(event)
    text = message_for(event)
    if text is None:
        return {"status": "ignored_event_type"}
    wa_id = "".join(ch for ch in (event.customer_phone or "") if ch.isdigit())
    async with get_sessionmaker()() as session, session.begin():
        inserted = (await session.execute(
            pg_insert(ProcessedEvent).values(event_id=event.event_id, event_type=event.event, order_no=event.order_no)
            .on_conflict_do_nothing(index_elements=["event_id"]).returning(ProcessedEvent.event_id)
        )).scalar_one_or_none()
        if inserted is None:
            logger.info("Duplicate mybill event ignored", extra={"fields": {"event_id": event.event_id}})
            return {"status": "duplicate"}
        conversation = (await session.execute(
            select(Conversation).where(Conversation.channel == "whatsapp", Conversation.external_user_id == wa_id)
        )).scalar_one_or_none()
        if conversation is None:
            logger.warning("Payment event for unknown customer", extra={"fields": {"order_no": event.order_no}})
            status = "unknown_customer"
        else:
            await repo.lock_conversation(session, conversation.id)
            messenger = WorkerMessenger(session, conversation)
            if not await messenger.send_text(text):
                raise RuntimeError(f"WhatsApp send failed: {messenger.error}")
            logger.info("PAYMENT_EVENT_SENT", extra={"fields": {"event": event.event, "order_no": event.order_no}})
            status = "sent"
    if event.event == "order.paid" and event.order_type == "DROPSHIP" and status != "duplicate":
        await _tell_owners_paid(event)
    if event.event == "order.paid" and event.order_type in GOODS_TYPES and status != "duplicate":
        await staff_alerts.alert_dispatch(event.order_no, event.customer_name, event.customer_phone, event.amount,
                                          event.items, event.delivery_address, firm=firms.for_schema(event.tenant),
                                          dropship=event.order_type == "DROPSHIP")
    if event.event == "order.paid" and event.exact_measurements_required:
        design = " ".join(p for p in (event.design_code, event.design_name) if p) or None
        await staff_alerts.alert_measurement_call(event.order_no, event.customer_name, event.customer_phone,
                                                  design, event.amount)
    return {"status": status}


OWNER_MESSAGE = "owner.message"


async def _tell_owners_paid(event: PaymentEvent) -> None:
    """A dropship order is paid: "✅ {code} paid by {customer}. Order from {supplier} ({phone}). Ship to: {address}"
    to the owner who forwarded that post (each owner number is its own user); every owner number only when mybill
    doesn't know who (a collection made by hand). Best effort."""
    from app import dropship_intake
    who = (event.customer_name or "a customer") + (f" (+{event.customer_phone.lstrip('+')})" if event.customer_phone else "")
    supplier = (event.supplier_name or "the supplier") + (f" ({event.supplier_phone})" if event.supplier_phone else "")
    text = (f"✅ {event.items or event.order_no} paid by {who} – {format_rupees(event.amount) if event.amount else ''} "
            f"(order {event.order_no}).\nOrder from {supplier}.\nShip to: {event.delivery_address or '-'}")
    if event.owner_phone:
        owners = {event.owner_phone}
    else:
        try:
            owners = await dropship_intake.owner_numbers()
        except Exception:  # noqa: BLE001
            owners = dropship_intake.cached_owner_numbers()
    for owner in owners:
        result = await whatsapp_client.send_text(owner, text)
        if not result.ok:
            logger.warning("Owner paid message not sent", extra={"fields": {"order_no": event.order_no, "error": result.error}})


async def _handle_owner(event: PaymentEvent) -> dict[str, Any]:
    """A message from mybill to the owner about a forwarded supplier post (they just messaged us, so the 24 h window is
    open). Once per event_id; raises on a failed send so mybill retries."""
    to = "".join(ch for ch in (event.customer_phone or "") if ch.isdigit())
    if not to or not event.text:
        return {"status": "invalid"}
    if not await _first_time(event):
        return {"status": "duplicate"}
    if event.image_url:
        await whatsapp_client.send_image(to, event.image_url)   # best effort: the list still goes out without it
    if event.kind == "list" and event.sections:
        result = await whatsapp_client.send_list(to, event.text, event.button_text or "Choose", event.sections)
    elif event.kind == "buttons" and event.buttons:
        result = await whatsapp_client.send_buttons(to, event.text, event.buttons)
    else:
        result = await whatsapp_client.send_text(to, event.text)
    if not result.ok:
        async with get_sessionmaker()() as session, session.begin():   # let mybill's retry send it again
            await session.execute(ProcessedEvent.__table__.delete().where(ProcessedEvent.event_id == event.event_id))
        raise RuntimeError(f"owner message not sent: {result.error}")
    logger.info("OWNER_MESSAGE_SENT", extra={"fields": {"event_id": event.event_id, "kind": event.kind}})
    return {"status": "sent"}


async def _handle_courier(event: PaymentEvent) -> dict[str, Any]:
    """Courier update to the customer, once. Inside her 24 h window: a normal message in the chat. Outside it: the
    approved shipping template; if that fails, the dispatch is alerted to staff so they forward the tracking link.
    Never raises (a closed window is not worth mybill retrying)."""
    settings = get_settings()
    wa_id = "".join(ch for ch in (event.customer_phone or "") if ch.isdigit())
    if len(wa_id) == 10:
        wa_id = "91" + wa_id
    status = "unknown_customer"
    async with get_sessionmaker()() as session, session.begin():
        inserted = (await session.execute(
            pg_insert(ProcessedEvent).values(event_id=event.event_id, event_type=event.event, order_no=event.order_no)
            .on_conflict_do_nothing(index_elements=["event_id"]).returning(ProcessedEvent.event_id)
        )).scalar_one_or_none()
        if inserted is None:
            return {"status": "duplicate"}
        conversation = (await session.execute(
            select(Conversation).where(Conversation.channel == "whatsapp", Conversation.external_user_id == wa_id)
        )).scalar_one_or_none()
        if conversation is not None:
            await repo.lock_conversation(session, conversation.id)
            last = conversation.last_customer_message_at
            if last is not None and datetime.now(timezone.utc) - last <= REPLY_WINDOW:
                messenger = WorkerMessenger(session, conversation)
                status = "sent" if await messenger.send_text(courier_text(event)) else "send_failed"
            else:
                params = [_first_name(event.customer_name) or "there", event.order_no, courier_status(event),
                          event.tracking_url or "-"]
                result = await whatsapp_client.send_template(wa_id, settings.shipping_update_template,
                                                             settings.shipping_update_template_lang, params)
                if result.ok:
                    repo.add_outbound_message(session, conversation.id, courier_text(event), result.message_id,
                                              {"template": settings.shipping_update_template}, author="bot")
                    status = "sent_template"
                else:
                    status = "window_closed"
    logger.info("COURIER_EVENT", extra={"fields": {"event": event.event, "order_no": event.order_no, "status": status}})
    if event.event == DISPATCHED and status not in ("sent", "sent_template"):
        await staff_alerts.send_alert(
            event.customer_name or "Customer", event.customer_phone or "-",
            f"🚚 order {event.order_no} shipped ({_courier_name(event)} AWB {event.awb}) but WhatsApp couldn't message "
            f"her ({status}). Please send her the tracking link: {event.tracking_url}",
            event.tracking_url or "-", firm=firms.for_schema(event.tenant))
    return {"status": status}

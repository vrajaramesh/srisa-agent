"""Payment events from mybill-service (POST /internal/events) -> a WhatsApp message to the customer.

mybill owns payments; it calls us after a Razorpay webhook (order.paid, payment_link.expired/cancelled).
Deduped by event_id in agent.processed_events, recorded in the same transaction as the message, so a retry
after a failed send re-sends and a duplicate after success is ignored.
"""

import logging
from datetime import date
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import repositories as repo
from app import staff_alerts
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
        text = (f"✅ Payment received for order {event.order_no}{amount}. {thanks}"
                f"Your outfit is now with our tailor 🧵" + (f" - estimated delivery {eta}." if eta else "."))
        if event.exact_measurements_required:
            phone = f" on {event.customer_phone}" if event.customer_phone else ""
            text += f"\n📏 Our tailor will call you{phone} shortly to take your exact measurements."
        return text
    if event.event == "payment_link.expired":
        return f"⏰ The payment link for order {event.order_no} has expired. Reply \"pay\" and I'll send you a new one."
    if event.event == "payment_link.cancelled":
        return f"The payment link for order {event.order_no} was cancelled. Reply \"pay\" if you'd like a new link."
    return None


async def handle(event: PaymentEvent) -> dict[str, Any]:
    """Send the message once. Returns {"status": ...}; raises RuntimeError if WhatsApp failed (-> 500, mybill retries).
    A paid order that needs exact measurements also alerts the staff (once, after the event is recorded)."""
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
    if event.event == "order.paid" and event.exact_measurements_required:
        design = " ".join(p for p in (event.design_code, event.design_name) if p) or None
        await staff_alerts.alert_measurement_call(event.order_no, event.customer_name, event.customer_phone,
                                                  design, event.amount)
    return {"status": status}

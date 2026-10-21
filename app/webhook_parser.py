"""Pure functions that pull the interesting parts out of a WhatsApp webhook payload.

Payload shape (WhatsApp Cloud API):

    {
      "object": "whatsapp_business_account",
      "entry": [{
        "id": "<WABA id>",
        "changes": [{
          "field": "messages",
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "...", "phone_number_id": "..."},
            "contacts": [{"profile": {"name": "Asha"}, "wa_id": "919876543210"}],
            "messages": [{"from": "919876543210", "id": "wamid...", "timestamp": "...",
                          "type": "text", "text": {"body": "Hi"}}],
            "statuses": [{"id": "wamid...", "status": "delivered", "recipient_id": "..."}]
          }
        }]
      }]
    }

`messages` = what customers sent us, `statuses` = sent/delivered/read receipts for
messages WE sent. One POST can batch several entries/changes, so iterate everything.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Referral:
    """Click-to-WhatsApp ad / post the customer tapped to start the chat (`messages[].referral`).

    Present only on the first message after the tap. source_type is "ad" or "post"; ctwa_clid is Meta's click id
    (missing for some placements); welcome_message is the greeting the customer already saw in WhatsApp.
    """

    source_id: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    headline: str | None = None
    body: str | None = None
    media_type: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    thumbnail_url: str | None = None
    ctwa_clid: str | None = None
    welcome_text: str | None = None
    # Not from Meta: guessed from Meta's default ad greeting when the referral was missing (see ad_entry).
    inferred: bool = False

    @property
    def is_ad(self) -> bool:
        return (self.source_type or "").lower() == "ad"


@dataclass(frozen=True)
class IncomingMessage:
    """A customer message, normalised."""

    id: str
    from_: str  # sender's WhatsApp id (phone number in international format, no '+')
    type: str  # text, image, audio, video, document, sticker, location, button, interactive, ...
    timestamp: str | None = None
    text: str | None = None  # only for type == "text"
    profile_name: str | None = None  # from value.contacts[]
    phone_number_id: str | None = None  # which of our numbers received it
    reply_id: str | None = None  # id of a tapped reply button / list row; its title is in text
    referral: Referral | None = None  # set when the chat was opened from a Click-to-WhatsApp ad / post
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)  # original JSON


@dataclass(frozen=True)
class StatusUpdate:
    """Delivery/read receipt for a message we sent."""

    id: str
    status: str  # sent | delivered | read | failed
    recipient_id: str | None = None


@dataclass
class ParsedWebhook:
    """Everything extracted from one webhook POST."""

    messages: list[IncomingMessage] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)


def iter_values(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every `entry[].changes[].value` dict in the payload."""
    values: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value")
            if isinstance(value, dict):
                values.append(value)
    return values


def _contact_names(value: dict[str, Any]) -> dict[str, str]:
    """Map wa_id -> profile name from `value.contacts[]`."""
    names: dict[str, str] = {}
    for contact in value.get("contacts") or []:
        wa_id = contact.get("wa_id")
        name = (contact.get("profile") or {}).get("name")
        if wa_id and name:
            names[wa_id] = name
    return names


def _parse_message(raw: dict[str, Any], names: dict[str, str], phone_number_id: str | None) -> IncomingMessage | None:
    """Normalise one raw message; returns None if it lacks an id or sender."""
    msg_id, sender = raw.get("id"), raw.get("from")
    if not msg_id or not sender:
        return None
    msg_type = raw.get("type", "unknown")
    text = (raw.get("text") or {}).get("body") if msg_type == "text" else None
    reply_id, reply_title = _reply(raw, msg_type)
    if reply_title is not None:
        text = reply_title
    return IncomingMessage(
        id=msg_id,
        from_=sender,
        type=msg_type,
        timestamp=raw.get("timestamp"),
        text=text,
        profile_name=names.get(sender),
        phone_number_id=phone_number_id,
        reply_id=reply_id,
        referral=parse_referral(raw.get("referral")),
        raw=raw,
    )


def _str(value: Any) -> str | None:
    """Non-empty string or None (ids sometimes arrive as numbers)."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def parse_referral(raw: Any) -> Referral | None:
    """`messages[].referral` -> Referral, or None if absent/malformed."""
    if not isinstance(raw, dict) or not raw:
        return None
    welcome = raw.get("welcome_message")
    return Referral(
        source_id=_str(raw.get("source_id")),
        source_type=_str(raw.get("source_type")),
        source_url=_str(raw.get("source_url")),
        headline=_str(raw.get("headline")),
        body=_str(raw.get("body")),
        media_type=_str(raw.get("media_type")),
        image_url=_str(raw.get("image_url")),
        video_url=_str(raw.get("video_url")),
        thumbnail_url=_str(raw.get("thumbnail_url")),
        ctwa_clid=_str(raw.get("ctwa_clid")),
        welcome_text=_str(welcome.get("text")) if isinstance(welcome, dict) else None,
    )


def _reply(raw: dict[str, Any], msg_type: str) -> tuple[str | None, str | None]:
    """(id, title) of a tapped reply.

    interactive.button_reply / interactive.list_reply = taps on our reply buttons / list rows;
    legacy "button" = a quick-reply button on a template message (payload is its id).
    """
    if msg_type == "interactive":
        interactive = raw.get("interactive") or {}
        chosen = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return chosen.get("id"), chosen.get("title")
    if msg_type == "button":
        button = raw.get("button") or {}
        return button.get("payload"), button.get("text")
    return None, None


def parse_webhook(payload: dict[str, Any]) -> ParsedWebhook:
    """Extract all customer messages and status receipts from a webhook payload."""
    result = ParsedWebhook()
    for value in iter_values(payload):
        names = _contact_names(value)
        phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
        for raw in value.get("messages") or []:
            msg = _parse_message(raw, names, phone_number_id)
            if msg:
                result.messages.append(msg)
        for raw in value.get("statuses") or []:
            if raw.get("id") and raw.get("status"):
                result.statuses.append(
                    StatusUpdate(id=raw["id"], status=raw["status"], recipient_id=raw.get("recipient_id"))
                )
    return result

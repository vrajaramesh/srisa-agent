"""Channel-neutral message types and the ChannelAdapter interface.

The worker and the agent tools only talk to a ChannelAdapter (through the worker's Messenger), so the same
agent, flow, tools and safety rules work on WhatsApp and Instagram.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from app.webhook_parser import Referral
from app.whatsapp_client import SendResult

InboundType = Literal["text", "image", "audio", "video", "share", "interactive", "other"]
ChoiceStyle = Literal["buttons", "list"]

__all__ = ["Attachment", "Choice", "ChannelAdapter", "NormalizedInbound", "SendResult"]


class Choice(TypedDict, total=False):
    id: str
    title: str
    description: str


@dataclass(frozen=True)
class Attachment:
    type: str                      # image | audio | video | ig_reel | share | ...
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class NormalizedInbound:
    """One customer message (or echo of a message the business sent), in channel-neutral form."""

    channel: str                   # whatsapp | instagram
    external_user_id: str          # wa_id or Instagram-scoped user id (IGSID)
    external_message_id: str       # wamid / Instagram mid
    timestamp: str | None          # unix seconds, as text
    type: InboundType
    text: str | None = None
    tapped_id: str | None = None   # id of a tapped button / quick reply / postback
    tapped_title: str | None = None
    attachments: tuple[Attachment, ...] = ()
    referral: Referral | None = None
    is_echo: bool = False          # sent by the business account (by us, or by staff in the app)
    profile_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


class ChannelAdapter(ABC):
    """Sends to one customer on one channel. Implementations never raise for API errors: they return a
    failed SendResult (and log)."""

    name: str
    # False = images can't carry a caption (Instagram): the Messenger sends the caption as its own text.
    image_captions: bool = True

    @abstractmethod
    def split_text(self, text: str) -> list[str]:
        """Split a reply into messages the channel accepts."""

    @abstractmethod
    async def send_text(self, to: str, text: str) -> SendResult: ...

    @abstractmethod
    async def send_image(self, to: str, url: str, caption: str | None = None) -> SendResult: ...

    @abstractmethod
    async def send_choices(self, to: str, body: str, choices: list[Choice], style: ChoiceStyle = "buttons",
                           button_text: str = "Choose", section_title: str | None = None) -> SendResult:
        """Offer choices. The result's data["offered"] lists what was offered; data["fallback"] is set when the
        channel had to fall back to numbered plain text (replies like "2" are then mapped back by the worker)."""

    @abstractmethod
    async def send_link_button(self, to: str, body: str, button_text: str, url: str) -> SendResult: ...

    @abstractmethod
    async def mark_seen(self, to: str, message_id: str | None) -> SendResult: ...

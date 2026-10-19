"""In-memory OrderStore and Messenger, for scripts/chat_cli.py and tests (no database, no WhatsApp)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.models import DRAFT_OPEN, Conversation, Message, OrderDraft


class MemoryStore:
    """orders.OrderStore keeping drafts in a dict."""

    def __init__(self) -> None:
        self.drafts: dict[uuid.UUID, OrderDraft] = {}
        self.ref_codes: dict[str, dict[str, Any]] = {}

    async def new_draft(self, conversation: Conversation) -> OrderDraft:
        draft = OrderDraft(id=uuid.uuid4(), conversation_id=conversation.id, status=DRAFT_OPEN, version=1,
                           option_ids=[], qty=1)
        self.drafts[draft.id] = draft
        return draft

    async def get_draft(self, draft_id: uuid.UUID) -> OrderDraft | None:
        return self.drafts.get(draft_id)

    async def latest_order_no(self, conversation: Conversation) -> str | None:
        submitted = [d for d in self.drafts.values() if d.conversation_id == conversation.id and d.mybill_order_no]
        return submitted[-1].mybill_order_no if submitted else None

    async def create_ref_code(self, conversation: Conversation, design_code: str | None) -> str:
        from app.agent import ig_handoff
        code = ig_handoff.new_ref_code()
        self.ref_codes[code] = {"ig_user_id": conversation.external_user_id, "design_code": design_code}
        return code

    def open_draft(self, conversation_id: uuid.UUID) -> OrderDraft | None:
        return next((d for d in self.drafts.values()
                     if d.conversation_id == conversation_id and d.status == DRAFT_OPEN), None)


@dataclass
class RecordingMessenger:
    """orders.Messenger that records every message as an outbound Message (and optionally prints it)."""

    printer: Callable[[str, dict[str, Any]], None] | None = None
    fail: bool = False
    sent: list[dict[str, Any]] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)  # outbound Message objects, in order

    def _record(self, kind: str, text: str, payload: dict[str, Any], meta: dict | None) -> bool:
        if self.fail:
            return False
        entry = {"kind": kind, "text": text, **payload, "meta": meta or {}}
        self.sent.append(entry)
        self.messages.append(Message(id=uuid.uuid4(), direction="out", type=kind if kind != "buttons" and kind != "list"
                                     else "interactive", text=text, raw=meta or {}, status="sent",
                                     created_at=datetime.now(timezone.utc)))
        if self.printer:
            self.printer(kind, entry)
        return True

    async def send_text(self, text: str) -> bool:
        return self._record("text", text, {}, None)

    async def send_image(self, url: str, caption: str | None) -> bool:
        return self._record("image", f"[photo] {caption or url}", {"url": url, "caption": caption}, None)

    async def send_buttons(self, body: str, buttons: list[dict[str, str]], meta: dict | None = None) -> bool:
        return self._record("buttons", body, {"buttons": buttons}, meta)

    async def send_list(self, body: str, button_text: str, sections: list[dict[str, Any]],
                        meta: dict | None = None) -> bool:
        return self._record("list", body, {"button_text": button_text, "sections": sections}, meta)

    async def send_link_button(self, body: str, button_text: str, url: str) -> bool:
        return self._record("link", body, {"button_text": button_text, "url": url}, None)

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["kind"] == kind]

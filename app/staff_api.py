"""Staff inbox API for mybill-ui "Customer chats" (called by mybill-service, never by browsers).

Auth: Google ID token of mybill-service's service account (audience = this service's URL), or the admin key /
an ADMIN_ALLOWED_EMAILS token for manual use.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select

from app import repositories as repo
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import INBOUND, Conversation, Message
from app.security import verify_admin, verify_tasks_token
from app.worker import WorkerMessenger

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/staff")

REPLY_WINDOW = timedelta(hours=24)


async def _authorize(request: Request) -> None:
    settings = get_settings()
    auth = request.headers.get("Authorization")
    ok = await run_in_threadpool(verify_tasks_token, auth, settings.tasks_target_url, settings.mybill_events_sa_email)
    if not ok:
        key = settings.admin_api_key.get_secret_value() if settings.admin_api_key else None
        ok = await run_in_threadpool(verify_admin, auth, key, settings.tasks_target_url, settings.admin_email_set)
    if not ok:
        raise HTTPException(status_code=403, detail="forbidden")


def _window_open(conversation: Conversation, now: datetime | None = None) -> bool:
    last = conversation.last_customer_message_at
    return last is not None and (now or datetime.now(timezone.utc)) - last <= REPLY_WINDOW


def _summary(c: Conversation, last: Message | None, waiting: int) -> dict[str, Any]:
    return {
        "id": str(c.id), "channel": c.channel, "external_user_id": c.external_user_id,
        "customer_name": c.customer_name, "profile_username": c.profile_username,
        "phone": ("+" + c.external_user_id) if c.channel == "whatsapp" else None,
        "needs_human": bool(c.needs_human), "needs_human_reason": c.needs_human_reason,
        "needs_human_since": c.needs_human_since, "stage": c.stage, "current_design": c.current_product_code,
        "mybill_customer_id": c.mybill_customer_id, "last_customer_message_at": c.last_customer_message_at,
        "reply_window_open": _window_open(c), "waiting_messages": waiting,
        "last_message": None if last is None else {"direction": last.direction, "author": last.author,
                                                    "text": (last.text or f"[{last.type}]")[:160],
                                                    "at": last.created_at},
    }


async def _waiting_counts(session, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Customer messages received since the handover (the ones the bot left for the team)."""
    if not ids:
        return {}
    rows = await session.execute(
        select(Message.conversation_id, func.count())
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Message.conversation_id.in_(ids), Message.direction == INBOUND,
               Conversation.needs_human_since.is_not(None), Message.created_at >= Conversation.needs_human_since)
        .group_by(Message.conversation_id))
    return dict(rows.all())


async def _last_messages(session, ids: list[uuid.UUID]) -> dict[uuid.UUID, Message]:
    if not ids:
        return {}
    latest = (select(Message.conversation_id, func.max(Message.created_at).label("at"))
              .where(Message.conversation_id.in_(ids)).group_by(Message.conversation_id).subquery())
    rows = await session.execute(select(Message).join(latest, and_(Message.conversation_id == latest.c.conversation_id,
                                                                    Message.created_at == latest.c.at)))
    return {m.conversation_id: m for m in rows.scalars().all()}


@router.get("/conversations")
async def list_conversations(request: Request, status: str = Query("waiting", pattern="^(waiting|all)$"),
                             limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """waiting = handed to the team (longest waiting first); all = most recently active."""
    await _authorize(request)
    async with get_sessionmaker()() as session:
        stmt = select(Conversation)
        stmt = (stmt.where(Conversation.needs_human).order_by(Conversation.needs_human_since.asc().nulls_first())
                if status == "waiting" else stmt.order_by(Conversation.updated_at.desc()))
        conversations = list((await session.execute(stmt.limit(limit))).scalars().all())
        ids = [c.id for c in conversations]
        last, waiting = await _last_messages(session, ids), await _waiting_counts(session, ids)
        return [_summary(c, last.get(c.id), waiting.get(c.id, 0)) for c in conversations]


@router.get("/conversations/count")
async def waiting_count(request: Request) -> dict[str, int]:
    await _authorize(request)
    async with get_sessionmaker()() as session:
        n = (await session.execute(select(func.count()).select_from(Conversation).where(Conversation.needs_human))).scalar_one()
    return {"waiting": n}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: uuid.UUID, request: Request,
                           limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    await _authorize(request)
    async with get_sessionmaker()() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="not_found")
        rows = (await session.execute(select(Message).where(Message.conversation_id == conversation_id)
                                      .order_by(Message.created_at.desc(), Message.id.desc()).limit(limit))).scalars().all()
        waiting = await _waiting_counts(session, [conversation_id])
        messages = [{"id": str(m.id), "direction": m.direction,
                     "author": m.author or ("customer" if m.direction == INBOUND else "bot"),
                     "type": m.type, "text": m.text, "status": m.status, "at": m.created_at,
                     "image_url": _image_url(m)} for m in reversed(rows)]
        return {**_summary(conversation, rows[0] if rows else None, waiting.get(conversation_id, 0)),
                "messages": messages}


def _image_url(m: Message) -> str | None:
    """Customer photos (WhatsApp media needs a download; Instagram gives a URL) and our own photos."""
    raw = m.raw or {}
    if m.direction != INBOUND and m.type == "image" and (m.text or "").startswith("[photo] http"):
        return m.text[len("[photo] "):]
    for a in ((raw.get("message") or {}).get("attachments") or []):
        if a.get("type") == "image":
            return (a.get("payload") or {}).get("url")
    return None


class StaffReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    staff_name: str | None = Field(default=None, max_length=100)


@router.post("/conversations/{conversation_id}/reply")
async def reply(conversation_id: uuid.UUID, body: StaffReply, request: Request) -> dict[str, Any]:
    """Send a staff message from the shop's account. The bot stays off for this chat until it's resolved."""
    await _authorize(request)
    async with get_sessionmaker()() as session, session.begin():
        await repo.lock_conversation(session, conversation_id)
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="not_found")
        if not _window_open(conversation):
            raise HTTPException(status_code=409, detail={
                "code": "WINDOW_CLOSED",
                "message": "More than 24 hours since the customer's last message: WhatsApp/Instagram only allow a "
                           "reply after she writes again."})
        messenger = WorkerMessenger(session, conversation, author="staff")
        if not await messenger.send_text(body.text.strip()):
            raise HTTPException(status_code=502, detail={"code": "SEND_FAILED", "message": messenger.error})
        if not conversation.needs_human:
            conversation.needs_human, conversation.needs_human_reason = True, "staff_reply"
        logger.info("STAFF_REPLIED", extra={"fields": {"conversation_id": str(conversation_id),
                                                       "staff": body.staff_name, "channel": conversation.channel}})
    return {"sent": True}


@router.post("/conversations/{conversation_id}/resolve")
async def resolve(conversation_id: uuid.UUID, request: Request) -> dict[str, Any]:
    """Issue handled: the AI answers this customer again from her next message."""
    return await _set_needs_human(conversation_id, request, False, None)


@router.post("/conversations/{conversation_id}/keep")
async def keep_with_team(conversation_id: uuid.UUID, request: Request) -> dict[str, Any]:
    """Keep the bot off (e.g. a longer case)."""
    return await _set_needs_human(conversation_id, request, True, "with_team")


async def _set_needs_human(conversation_id: uuid.UUID, request: Request, value: bool, reason: str | None) -> dict[str, Any]:
    await _authorize(request)
    async with get_sessionmaker()() as session, session.begin():
        await repo.lock_conversation(session, conversation_id)
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="not_found")
        if value and conversation.needs_human:
            pass   # already with the team: keep the original reason and waiting time
        else:
            conversation.needs_human, conversation.needs_human_reason = value, reason
        logger.info("STAFF_SET_NEEDS_HUMAN", extra={"fields": {"conversation_id": str(conversation_id), "value": value}})
        return {"needs_human": bool(conversation.needs_human), "needs_human_reason": conversation.needs_human_reason}

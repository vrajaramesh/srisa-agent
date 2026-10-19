"""FastAPI app: health check, WhatsApp webhook and the Cloud Tasks worker endpoint."""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app import events, mybill_client, queue, worker, whatsapp_client
from app.channels import instagram
from app import repositories as repo
from app.config import get_settings
from app.db import dispose_db, get_sessionmaker, init_db
from app.logging_setup import setup_logging
from app.models import RECEIVED, SKIPPED, Conversation, RawWebhook
from app.security import tokens_match, verify_admin, verify_signature, verify_tasks_token
from app.webhook_parser import IncomingMessage, ParsedWebhook, parse_webhook

logger = logging.getLogger("srisa_agent")

CHANNEL = "whatsapp"
IG_CHANNEL = "instagram"

# At import time, so uvicorn's own startup lines are already JSON.
setup_logging(get_settings().log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the DB pool and the shared HTTP client for the app's lifetime."""
    settings = get_settings()
    init_db(settings.database_url.get_secret_value(), settings.db_schema)
    await whatsapp_client.start_client()
    await mybill_client.start_client(settings)
    logger.info("srisa-agent started", extra={"fields": {
        "use_cloud_tasks": settings.use_cloud_tasks, "llm_model": settings.llm_model,
        "mybill_configured": bool(settings.mybill_base_url)}})
    try:
        yield
    finally:
        await mybill_client.close_client()
        await whatsapp_client.close_client()
        await instagram.close_client()
        await dispose_db()


app = FastAPI(title="srisa-agent", lifespan=lifespan)

from app.staff_api import router as staff_router  # noqa: E402

app.include_router(staff_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/webhook/whatsapp")
async def verify_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Meta's one-time subscription handshake.

    When you save the webhook URL in the Meta dashboard, Meta calls this with
    hub.mode=subscribe, your verify token and a random challenge. Echoing the challenge
    back (as plain text, not JSON) proves we own the endpoint.
    """
    if mode == "subscribe" and tokens_match(token, get_settings().wa_verify_token.get_secret_value()):
        logger.info("Webhook verified")
        return PlainTextResponse(challenge or "", status_code=200)
    logger.warning("Webhook verification failed", extra={"fields": {"mode": mode}})
    return Response(status_code=403)


@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request, background: BackgroundTasks) -> Response:
    """Receive WhatsApp events: verify -> save -> enqueue -> 200. No replying here."""
    raw_body = await request.body()  # raw bytes - the signature is over these exact bytes
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw_body, signature, get_settings().wa_app_secret.get_secret_value()):
        logger.warning(
            "Rejected webhook: missing or invalid signature",
            extra={"fields": {"has_signature": signature is not None}},
        )
        return Response(status_code=401)

    try:
        payload = json.loads(raw_body)
    except ValueError:
        logger.warning("Rejected webhook: body is not valid JSON")
        return Response(status_code=400)

    try:
        conversation_ids = await store_webhook(parse_webhook(payload))
    except (SQLAlchemyError, OSError):
        # Not saved, so ask Meta to redeliver later instead of losing the message.
        logger.exception("Failed to store webhook")
        return Response(status_code=500)

    await queue.enqueue(conversation_ids, background)  # never raises
    return Response(status_code=200)


@app.get("/wa/{ref}")
async def whatsapp_order_redirect(ref: str) -> Response:
    """Short "Order on WhatsApp" link sent on Instagram: redirects to wa.me with the pre-typed order message."""
    from fastapi.responses import RedirectResponse
    from app.agent import ig_handoff
    code = ref.upper()
    design = None
    if ig_handoff.REF_RE.fullmatch(code):
        try:
            async with get_sessionmaker()() as session:
                row = await repo.get_ref_code(session, code)
                design = row.design_code if row else None
        except (SQLAlchemyError, OSError):
            logger.exception("Ref lookup failed; redirecting without the design")
    else:
        code = ""
    text = ig_handoff.prefilled_text(design, code) if code else "Hi SRISA! I'd like to place an order 🛍️"
    url = ig_handoff.whatsapp_link(get_settings().whatsapp_order_number, text)
    logger.info("WhatsApp order link opened", extra={"fields": {"ref": code or None, "design": design}})
    return RedirectResponse(url, status_code=302)


@app.get("/webhook/instagram")
async def verify_instagram_webhook(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Meta's subscription handshake for the Instagram webhook (IG_VERIFY_TOKEN, else WA_VERIFY_TOKEN)."""
    if mode == "subscribe" and tokens_match(token, get_settings().ig_verify_token_value):
        logger.info("Instagram webhook verified")
        return PlainTextResponse(challenge or "", status_code=200)
    logger.warning("Instagram webhook verification failed", extra={"fields": {"mode": mode}})
    return Response(status_code=403)


ECHO_DELAY_SECONDS = 5


@app.post("/webhook/instagram")
async def receive_instagram_webhook(request: Request, background: BackgroundTasks) -> Response:
    """Instagram DMs: verify -> store (raw copy + parsed messages/echoes) -> enqueue -> 200.
    Echoes are processed ~5 s later, so our own sends are recorded before they're judged."""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    secrets = get_settings().ig_webhook_secrets
    if not any(verify_signature(raw_body, signature, secret) for secret in secrets):
        logger.warning("Rejected Instagram webhook: missing or invalid signature", extra={"fields": {
            "has_signature": signature is not None, "secrets_tried": len(secrets),
            "hint": "set IG_APP_SECRET to the Instagram app secret (Instagram > API setup) if this persists"}})
        return Response(status_code=401)
    try:
        payload = json.loads(raw_body)
    except ValueError:
        logger.warning("Rejected Instagram webhook: body is not valid JSON")
        return Response(status_code=400)
    try:
        conversation_ids, echo_conversation_ids = await store_instagram_webhook(payload)
    except (SQLAlchemyError, OSError):
        logger.exception("Failed to store Instagram webhook")
        return Response(status_code=500)
    await queue.enqueue(conversation_ids, background)
    await queue.enqueue(echo_conversation_ids, background, delay_seconds=ECHO_DELAY_SECONDS)
    entries = payload.get("entry") or [] if isinstance(payload, dict) else []
    logger.info("Instagram webhook stored", extra={"fields": {
        "object": payload.get("object") if isinstance(payload, dict) else None,
        "entries": len(entries),
        "kinds": sorted({k for e in entries if isinstance(e, dict) for k in e if k not in ("id", "time")})}})
    return Response(status_code=200)


async def store_instagram_webhook(payload: dict) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Save the raw body, customer messages and echoes in one transaction.
    Returns (conversations with new customer messages, conversations with new echoes)."""
    allowed = get_settings().allowed_ig_sender_set
    to_process: list[uuid.UUID] = []
    echoes: list[uuid.UUID] = []
    async with get_sessionmaker()() as session, session.begin():
        if get_settings().ig_capture_raw:
            session.add(RawWebhook(channel=IG_CHANNEL, payload=payload))
        for msg in instagram.parse_webhook(payload):
            if msg.is_echo:
                conversation_id = await repo.upsert_conversation_for(session, IG_CHANNEL, msg.external_user_id, None, None)
                if await repo.record_instagram_echo(session, conversation_id, msg.external_message_id, msg.text, msg.raw):
                    if conversation_id not in echoes:
                        echoes.append(conversation_id)
                continue
            conversation_id = await repo.upsert_conversation_for(
                session, IG_CHANNEL, msg.external_user_id, None, _instagram_time(msg.timestamp))
            is_allowed = not allowed or msg.external_user_id in allowed
            stored = await repo.insert_inbound(session, conversation_id, msg.external_message_id, msg.type, msg.text,
                                               msg.tapped_id, msg.raw, RECEIVED if is_allowed else SKIPPED)
            if stored is None:
                logger.info("Duplicate Instagram message ignored", extra={"fields": {"message_id": msg.external_message_id}})
                continue
            logger.info("Incoming Instagram message", extra={"fields": {
                "message_id": msg.external_message_id, "from": msg.external_user_id, "type": msg.type, "text": msg.text,
                "allowed": is_allowed}})
            if is_allowed and conversation_id not in to_process:
                to_process.append(conversation_id)
    return to_process, echoes


def _instagram_time(seconds: str | None) -> datetime:
    try:
        return datetime.fromtimestamp(int(seconds or ""), tz=timezone.utc)
    except (ValueError, OverflowError):
        return datetime.now(timezone.utc)


async def store_webhook(parsed: ParsedWebhook) -> list[uuid.UUID]:
    """Save messages and receipts in one transaction. Returns conversations needing work."""
    allowed = get_settings().allowed_sender_set
    to_process: list[uuid.UUID] = []
    async with get_sessionmaker()() as session, session.begin():
        for msg in parsed.messages:
            conversation_id = await repo.upsert_conversation(session, CHANNEL, msg)
            # Stored as "skipped" (never answered) so clearing ALLOWED_SENDERS later
            # doesn't make the bot reply to old messages.
            is_allowed = not allowed or msg.from_ in allowed
            stored = await repo.insert_inbound_message(
                session, conversation_id, msg, RECEIVED if is_allowed else SKIPPED
            )
            if stored is None:
                logger.info("Duplicate message ignored", extra={"fields": {"message_id": msg.id}})
                continue
            if msg.referral is not None:
                await repo.record_entry(session, conversation_id, stored, msg)
            log_incoming(msg)
            if not is_allowed:
                logger.info("Sender not in ALLOWED_SENDERS; not replying", extra={"fields": {"message_id": msg.id}})
            elif conversation_id not in to_process:
                to_process.append(conversation_id)

        for status in parsed.statuses:
            updated = await repo.apply_status(session, status)
            logger.debug(
                "Status update",
                extra={"fields": {"message_id": status.id, "status": status.status, "matched": updated}},
            )
    return to_process


def log_incoming(msg: IncomingMessage) -> None:
    """Log the key facts about an incoming customer message."""
    logger.info(
        "Incoming message",
        extra={
            "fields": {
                "message_id": msg.id,
                "from": msg.from_,
                "profile_name": msg.profile_name,
                "type": msg.type,
                "text": msg.text,
                **({"referral_source_type": msg.referral.source_type, "referral_source_id": msg.referral.source_id}
                   if msg.referral else {}),
            }
        },
    )


class ProcessMessageTask(BaseModel):
    """Body Cloud Tasks POSTs to the worker."""

    conversation_id: uuid.UUID


@app.post(queue.TASK_PATH)
async def process_message_task(request: Request, task: ProcessMessageTask) -> Response:
    """Cloud Tasks worker: answer one conversation's pending messages.

    500 on a failed send makes Cloud Tasks retry (per the queue's retry config).
    """
    settings = get_settings()
    if settings.use_cloud_tasks:
        authorized = await run_in_threadpool(
            verify_tasks_token,
            request.headers.get("Authorization"),
            settings.tasks_target_url.rstrip("/"),
            settings.tasks_service_account,
        )
        if not authorized:
            logger.warning("Rejected task: missing or invalid OIDC token")
            return Response(status_code=403)

    result = await worker.process_conversation(task.conversation_id)
    return JSONResponse(
        {"processed": result.processed, "failed": result.failed},
        status_code=500 if result.failed else 200,
    )


@app.post("/admin/conversations/{conversation_id}/resume")
async def resume_conversation(conversation_id: uuid.UUID, request: Request) -> Response:
    """Hand a conversation back to the bot after the team has dealt with it (needs_human -> false)."""
    settings = get_settings()
    authorized = await run_in_threadpool(
        verify_admin,
        request.headers.get("Authorization"),
        settings.admin_api_key.get_secret_value() if settings.admin_api_key else None,
        settings.tasks_target_url.rstrip("/"),
        settings.admin_email_set,
    )
    if not authorized:
        logger.warning("Rejected admin request", extra={"fields": {"path": request.url.path}})
        return Response(status_code=403)

    async with get_sessionmaker()() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return JSONResponse({"detail": "conversation not found"}, status_code=404)
        previous = conversation.needs_human_reason
        conversation.needs_human, conversation.needs_human_reason = False, None
    logger.info("Conversation resumed by admin",
                extra={"fields": {"conversation_id": str(conversation_id), "previous_reason": previous}})
    return JSONResponse({"conversation_id": str(conversation_id), "needs_human": False})


@app.post("/internal/events")
async def mybill_event(request: Request, event: events.PaymentEvent) -> Response:
    """Payment notifications from mybill-service, authenticated with its Google ID token."""
    settings = get_settings()
    authorized = await run_in_threadpool(
        verify_tasks_token,
        request.headers.get("Authorization"),
        settings.tasks_target_url.rstrip("/"),
        settings.mybill_events_sa_email,
    )
    if not authorized:
        logger.warning("Rejected /internal/events: missing or invalid ID token")
        return Response(status_code=403)
    try:
        result = await events.handle(event)
    except RuntimeError as exc:
        logger.error("Payment event not delivered", extra={"fields": {"event_id": event.event_id, "error": str(exc)}})
        return JSONResponse({"status": "send_failed"}, status_code=500)
    return JSONResponse(result)

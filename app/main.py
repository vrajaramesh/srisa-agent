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

from app import dropship_intake, events, firms, followups, insights, mybill_client, queue, retention, worker, whatsapp_client
from app.mybill_client import MybillRejected
from app.channels import instagram
from app import repositories as repo
from app.config import get_settings
from app.db import dispose_db, get_sessionmaker, init_db
from app.logging_setup import setup_logging
from app.models import RECEIVED, SKIPPED, Conversation, InstagramComment, RawWebhook
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    parsed = parse_webhook(payload)
    owner = await dropship_intake.owner_numbers() if parsed.messages else set()
    suppliers = (await dropship_intake.supplier_numbers()) - owner if parsed.messages else set()
    owner_payloads = [dropship_intake.payload(m, "owner" if m.from_ in owner else "supplier")
                      for m in parsed.messages if m.from_ in owner or m.from_ in suppliers]
    try:
        # owner and supplier messages are stored too, but never answered by the AI
        conversation_ids = await store_webhook(parsed, owner | suppliers)
    except (SQLAlchemyError, OSError):
        # Not saved, so ask Meta to redeliver later instead of losing the message.
        logger.exception("Failed to store webhook")
        return Response(status_code=500)

    await queue.enqueue(conversation_ids, background)  # never raises
    await queue.enqueue_owner(owner_payloads, background)  # never raises
    return Response(status_code=200)


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
        conversation_ids, echo_conversation_ids, comment_ids = await store_instagram_webhook(payload)
    except (SQLAlchemyError, OSError):
        logger.exception("Failed to store Instagram webhook")
        return Response(status_code=500)
    await queue.enqueue(conversation_ids, background)
    await queue.enqueue(echo_conversation_ids, background, delay_seconds=ECHO_DELAY_SECONDS)
    await queue.enqueue_comments(comment_ids, background)
    entries = payload.get("entry") or [] if isinstance(payload, dict) else []
    logger.info("Instagram webhook stored", extra={"fields": {
        "object": payload.get("object") if isinstance(payload, dict) else None,
        "entries": len(entries),
        "kinds": sorted({k for e in entries if isinstance(e, dict) for k in e if k not in ("id", "time")})}})
    return Response(status_code=200)


async def store_instagram_webhook(payload: dict) -> tuple[list[uuid.UUID], list[uuid.UUID], list[str]]:
    """Save the raw body, customer messages, echoes and comments in one transaction.
    Returns (conversations with new customer messages, conversations with new echoes, new comment ids)."""
    allowed = get_settings().allowed_ig_sender_set
    to_process: list[uuid.UUID] = []
    echoes: list[uuid.UUID] = []
    comments: list[str] = []
    async with get_sessionmaker()() as session, session.begin():
        if get_settings().ig_capture_raw:
            session.add(RawWebhook(channel=IG_CHANNEL, payload=payload))
        for comment in instagram.parse_comments(payload):
            inserted = (await session.execute(
                pg_insert(InstagramComment).values(comment_id=comment.comment_id, media_id=comment.media_id,
                                                   from_id=comment.from_id, username=comment.username,
                                                   text=comment.text, raw=comment.raw)
                .on_conflict_do_nothing(index_elements=["comment_id"]).returning(InstagramComment.comment_id)
            )).scalar_one_or_none()
            if inserted:
                comments.append(inserted)
        for msg in instagram.parse_webhook(payload):
            firm = firms.for_ig_account(msg.account_id).key   # which of our accounts (SRISA / Srihitha) she wrote to
            if msg.is_echo:
                conversation_id = await repo.upsert_conversation_for(session, IG_CHANNEL, msg.external_user_id, None,
                                                                     None, firm)
                if await repo.record_instagram_echo(session, conversation_id, msg.external_message_id, msg.text, msg.raw):
                    if conversation_id not in echoes:
                        echoes.append(conversation_id)
                continue
            conversation_id = await repo.upsert_conversation_for(
                session, IG_CHANNEL, msg.external_user_id, None, _instagram_time(msg.timestamp), firm)
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
    return to_process, echoes, comments


def _instagram_time(seconds: str | None) -> datetime:
    try:
        return datetime.fromtimestamp(int(seconds or ""), tz=timezone.utc)
    except (ValueError, OverflowError):
        return datetime.now(timezone.utc)


async def store_webhook(parsed: ParsedWebhook, owners: set[str] | None = None) -> list[uuid.UUID]:
    """Save messages and receipts in one transaction. Returns conversations needing work. owners = owner numbers
    (their forwarded posts go to the dropship intake; the AI never answers them)."""
    allowed = get_settings().allowed_sender_set
    staff = get_settings().all_staff_numbers | (owners or set())
    to_process: list[uuid.UUID] = []
    async with get_sessionmaker()() as session, session.begin():
        for msg in parsed.messages:
            conversation_id = await repo.upsert_conversation(session, CHANNEL, msg)
            # Stored as "skipped" (never answered) so clearing ALLOWED_SENDERS later
            # doesn't make the bot reply to old messages. Staff phones (alert numbers) never get bot replies:
            # they message the shop only to keep the 24 h window open for alerts.
            is_allowed = (not allowed or msg.from_ in allowed) and msg.from_ not in staff
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


async def _task_authorized(request: Request) -> bool:
    settings = get_settings()
    if not settings.use_cloud_tasks:
        return True
    return await run_in_threadpool(verify_tasks_token, request.headers.get("Authorization"),
                                   settings.tasks_target_url.rstrip("/"), settings.tasks_service_account)


@app.post(queue.OWNER_TASK_PATH)
async def owner_message_task(request: Request) -> Response:
    """Cloud Tasks: one of the owner's forwarded messages -> mybill dropship intake. 500 = retry."""
    if not await _task_authorized(request):
        return Response(status_code=403)
    p = await request.json()
    try:
        result = await dropship_intake.handle(p)
        if (batch_id := dropship_intake.needs_extraction(result)) is not None:
            await queue.enqueue_dropship_extract(batch_id)
        if (collection_id := dropship_intake.needs_publishing(result)) is not None:
            await queue.enqueue_dropship_publish(collection_id)
        if (job := dropship_intake.needs_boosting(result, p["from"])) is not None:
            await queue.enqueue_dropship_boost(job)
    except MybillRejected as exc:   # bad input: retrying won't help
        logger.error("Dropship intake rejected", extra={"fields": {"wa_message_id": p.get("wa_message_id"), "error": str(exc)}})
        return JSONResponse({"status": "rejected"})
    except Exception as exc:
        logger.error("Dropship intake failed", extra={"fields": {"wa_message_id": p.get("wa_message_id"), "error": repr(exc)[:300]}})
        return JSONResponse({"status": "retry"}, status_code=500)
    return JSONResponse(result)


@app.post(queue.DROPSHIP_EXTRACT_PATH)
async def dropship_extract_task(request: Request) -> Response:
    """Cloud Tasks: AI extraction of a post -> DRAFT collection in mybill (which tells the owner). 500 = retry."""
    if not await _task_authorized(request):
        return Response(status_code=403)
    batch_id = int((await request.json()).get("batch_id"))
    try:
        return JSONResponse(await dropship_intake.extract(batch_id))
    except MybillRejected as exc:
        logger.error("Dropship extraction rejected", extra={"fields": {"batch_id": batch_id, "error": str(exc)}})
        return JSONResponse({"status": "rejected"})
    except Exception as exc:
        logger.error("Dropship extraction failed", extra={"fields": {"batch_id": batch_id, "error": repr(exc)[:300]}})
        return JSONResponse({"status": "retry"}, status_code=500)


@app.post(queue.DROPSHIP_SUPPLIER_TIMEOUT_PATH)
async def dropship_supplier_timeout_task(request: Request) -> Response:
    """Cloud Tasks, 15 min after the supplier was asked: still no answer → ask the owner. 500 = retry."""
    if not await _task_authorized(request):
        return Response(status_code=403)
    from app import dropship_flow
    body = await request.json()
    try:
        return JSONResponse(await dropship_flow.supplier_timeout(uuid.UUID(body["conversation_id"]), body["item_code"]))
    except MybillRejected as exc:
        logger.error("Supplier timeout rejected", extra={"fields": {"item": body.get("item_code"), "error": str(exc)}})
        return JSONResponse({"status": "rejected"})
    except Exception as exc:
        logger.error("Supplier timeout failed", extra={"fields": {"item": body.get("item_code"), "error": repr(exc)[:300]}})
        return JSONResponse({"status": "retry"}, status_code=500)


@app.post(queue.DROPSHIP_BOOST_PATH)
async def dropship_boost_task(request: Request) -> Response:
    """Cloud Tasks: create the boost the owner chose. Always 200 - a retry could create a second ad."""
    if not await _task_authorized(request):
        return Response(status_code=403)
    job = await request.json()
    try:
        return JSONResponse(await dropship_intake.boost(job))
    except Exception as exc:  # noqa: BLE001
        logger.error("Dropship boost failed", extra={"fields": {"collection_id": job.get("collection_id"), "error": repr(exc)[:300]}})
        return JSONResponse({"status": "failed"})


@app.post(queue.DROPSHIP_PUBLISH_PATH)
async def dropship_publish_task(request: Request) -> Response:
    """Cloud Tasks: post an approved dropship collection (mybill tells the owner the links). 500 = retry."""
    if not await _task_authorized(request):
        return Response(status_code=403)
    collection_id = int((await request.json()).get("collection_id"))
    try:
        return JSONResponse(await dropship_intake.publish(collection_id))
    except MybillRejected as exc:
        logger.error("Dropship publishing rejected", extra={"fields": {"collection_id": collection_id, "error": str(exc)}})
        return JSONResponse({"status": "rejected"})
    except Exception as exc:
        logger.error("Dropship publishing failed", extra={"fields": {"collection_id": collection_id, "error": repr(exc)[:300]}})
        return JSONResponse({"status": "retry"}, status_code=500)


@app.post(queue.DROPSHIP_CLOSE_PATH)
async def dropship_close_task(request: Request) -> Response:
    """Cloud Tasks (delayed): close forwarded posts that have been quiet for dropship_idle_seconds."""
    if not await _task_authorized(request):
        return Response(status_code=403)
    try:
        result = await dropship_intake.close_quiet_batches()
        if result.get("awaiting_supplier_ids"):   # check again when the 2 h reminder is due
            await queue.enqueue_dropship_check(queue.REMINDER_DELAY)
        return JSONResponse(result)
    except Exception as exc:
        logger.error("Dropship close failed", extra={"fields": {"error": repr(exc)[:300]}})
        return JSONResponse({"status": "retry"}, status_code=500)


class ProcessCommentTask(BaseModel):
    comment_id: str


@app.post(queue.COMMENT_TASK_PATH)
async def process_comment_task(request: Request, task: ProcessCommentTask) -> Response:
    """Cloud Tasks worker: auto-reply to one Instagram comment (500 -> retried)."""
    settings = get_settings()
    if settings.use_cloud_tasks:
        authorized = await run_in_threadpool(verify_tasks_token, request.headers.get("Authorization"),
                                             settings.tasks_target_url.rstrip("/"), settings.tasks_service_account)
        if not authorized:
            return Response(status_code=403)
    from app import ig_comments
    outcome = await ig_comments.process_comment(task.comment_id)
    return JSONResponse({"outcome": outcome}, status_code=500 if outcome == ig_comments.FAILED else 200)


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


@app.post("/internal/jobs/followups")
async def followups_job(request: Request) -> Response:
    """Cloud Scheduler (every 30 min): reminders for quiet customers and unpaid orders (app/followups.py)."""
    settings = get_settings()
    authorized = settings.scheduler_sa_email and await run_in_threadpool(
        verify_tasks_token, request.headers.get("Authorization"), settings.tasks_target_url.rstrip("/"),
        settings.scheduler_sa_email)
    if not authorized:
        logger.warning("Rejected /internal/jobs/followups: missing or invalid ID token")
        return Response(status_code=403)
    stats = await followups.run()
    logger.info("Follow-up job done", extra={"fields": stats})
    stats["courier"] = await _courier_sync()
    return JSONResponse(stats)


async def _courier_sync() -> dict:
    """Courier status of shipped orders, per firm (mybill sends "out for delivery" / "delivered" via /internal/events)."""
    base = mybill_client.get_client()
    if base is None:
        return {}
    out: dict = {}
    for firm in firms.all_firms():
        try:
            out[firm.key] = await base.for_tenant(firm.schema).courier_sync()
        except Exception as exc:  # noqa: BLE001 - one firm failing must not stop the job
            logger.warning("Courier sync failed", extra={"fields": {"firm": firm.key, "error": type(exc).__name__}})
            out[firm.key] = {"error": type(exc).__name__}
    return out


@app.post("/internal/jobs/daily-report")
async def daily_report_job(request: Request) -> Response:
    """Cloud Scheduler (every evening): today's AI report (app/insights.py)."""
    settings = get_settings()
    authorized = settings.scheduler_sa_email and await run_in_threadpool(
        verify_tasks_token, request.headers.get("Authorization"), settings.tasks_target_url.rstrip("/"),
        settings.scheduler_sa_email)
    if not authorized:
        logger.warning("Rejected /internal/jobs/daily-report: missing or invalid ID token")
        return Response(status_code=403)
    return JSONResponse(await insights.run_daily_report(insights.today_ist()))


@app.post("/internal/jobs/retention")
async def retention_job(request: Request, dry_run: bool = False) -> Response:
    """Cloud Scheduler (every night): delete old chat data (periods in app/retention.py)."""
    settings = get_settings()
    authorized = settings.scheduler_sa_email and await run_in_threadpool(
        verify_tasks_token, request.headers.get("Authorization"), settings.tasks_target_url.rstrip("/"),
        settings.scheduler_sa_email)
    if not authorized:
        logger.warning("Rejected /internal/jobs/retention: missing or invalid ID token")
        return Response(status_code=403)
    return JSONResponse(await retention.run(dry_run=dry_run))


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

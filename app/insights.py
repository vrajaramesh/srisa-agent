"""Daily AI report: what happened in one IST day's WhatsApp/Instagram chats and sales, analysed by Claude.

run_daily_report(day):
  1. metrics from our tables (chats, ad visitors, handovers, reminders, Instagram comments) + mybill's day figures
     (orders, payments, website sales);
  2. a compact transcript of every chat active that day (customer / AI / team / reminder lines);
  3. one Claude call with a JSON schema, so the report always has the same sections;
  4. saved in agent.daily_reports (re-running a day replaces it).
Runs every evening (Cloud Scheduler -> /internal/jobs/daily-report) or from mybill-ui (Generate now).
"""

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import anthropic
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import mybill_client
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import INBOUND, Conversation, DailyReport, InstagramComment, Message

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
MAX_MESSAGES_PER_CHAT = 40
MAX_MESSAGE_CHARS = 300
MAX_TRANSCRIPT_CHARS = 240_000          # ~70k tokens: keeps one day's call well inside the context window
AREAS = ["instagram", "whatsapp", "website", "catalog", "pricing", "operations"]


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _list(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": _obj(item)}


S, I = {"type": "string"}, {"type": "integer"}
REPORT_SCHEMA = _obj({
    "headline": S,
    "summary": S,
    "sentiment": _obj({"positive": I, "neutral": I, "negative": I}),
    "top_questions": _list({"topic": S, "count": I, "example": S}),
    "objections": _list({"reason": S, "count": I, "example": S}),
    "design_feedback": _list({"design": S, "feedback": S}),
    "unmet_demand": _list({"request": S, "count": I}),
    "ai_issues": _list({"issue": S, "example": S, "fix": S}),
    "recommendations": _list({"area": {"type": "string", "enum": AREAS}, "priority": {"type": "string",
                              "enum": ["high", "medium", "low"]}, "action": S, "why": S}),
    "follow_up": _list({"customer": S, "chat_id": S, "reason": S}),
})

SYSTEM = """You are a sharp retail analyst for SRISA Fabrics, a Hyderabad boutique and fabric store. They design ladies' \
kurtis and dresses and custom-stitch them to the customer's size (delivered across India), and also sell fabrics by \
the metre. Customers find them through Instagram posts and Click-to-WhatsApp ads; an AI assistant answers on WhatsApp \
(and Instagram DMs), takes stitched orders with a Razorpay payment link, and sends fabric-only buyers to the website \
srisafabrics.com. The team steps in when the AI hands a chat over. Automatic reminders go to customers who went quiet.

You get one day's figures and every chat of that day. Write a practical report for the owner, who will use it to \
decide what to change in their Instagram ads/posts, WhatsApp selling, website, catalog and pricing.

Rules:
- Base every statement on the chats and figures given. Counts are numbers of customers (chats), estimated by you.
- Quote short real examples (customer wording, translated to English if needed, no phone numbers).
- objections: why people did not order (price, readymade expected, size, delivery time, just browsing, no reply...).
- unmet_demand: things customers asked for that we don't offer (other designs, kids sizes, readymade, COD...).
- ai_issues: places where the AI assistant answered badly, repeated itself, missed a question or handed over \
needlessly - with a concrete fix.
- recommendations: 3-8 specific, doable actions, most important first (e.g. "Add 'not readymade - stitched in 3 days' \
to the FRO-11 ad caption"), each with the evidence that supports it.
- follow_up: customers worth a personal message from the team (hot leads, unhappy customers, unanswered questions), \
with the chat_id exactly as given.
- headline: one line; summary: 3-5 sentences in plain English.
- If a section has nothing, return an empty list. Days with very few chats get a short report."""


def ist_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, IST)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _who(m: Message) -> str:
    if m.direction == INBOUND:
        return "Customer"
    if (m.raw or {}).get("followup"):
        return "Reminder"
    return "Team" if m.author == "staff" else "AI"


def _line(m: Message) -> str:
    text = (m.text or f"[{m.type}]").replace("\n", " ").strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "…"
    return f"[{m.created_at.astimezone(IST):%H:%M}] {_who(m)}: {text}"


async def collect(day: date) -> tuple[dict[str, Any], str, int]:
    """(metrics, transcript, chats in transcript) for one IST day."""
    start, end = ist_day_bounds(day)
    async with get_sessionmaker()() as session:
        messages = list((await session.execute(
            select(Message).where(Message.created_at >= start, Message.created_at < end)
            .order_by(Message.conversation_id, Message.created_at))).scalars().all())
        conv_ids = sorted({m.conversation_id for m in messages}, key=str)
        conversations = {c.id: c for c in (await session.execute(
            select(Conversation).where(Conversation.id.in_(conv_ids)))).scalars().all()} if conv_ids else {}
        comments = (await session.execute(
            select(InstagramComment.outcome, func.count()).where(InstagramComment.received_at >= start,
                                                                 InstagramComment.received_at < end)
            .group_by(InstagramComment.outcome))).all()

    by_conv: dict[Any, list[Message]] = {}
    for m in messages:
        by_conv.setdefault(m.conversation_id, []).append(m)
    staff = set(get_settings().staff_alert_number_list)
    by_conv = {k: v for k, v in by_conv.items()
               if k in conversations and conversations[k].external_user_id not in staff}

    def in_day(t: datetime | None) -> bool:
        return t is not None and start <= t < end

    reminders = [m for ms in by_conv.values() for m in ms if (m.raw or {}).get("followup")]
    replied_after_reminder = sum(
        1 for ms in by_conv.values()
        if any((m.raw or {}).get("followup") and any(n.direction == INBOUND and n.created_at > m.created_at for n in ms)
               for m in ms))
    convs = [conversations[k] for k in by_conv]
    metrics: dict[str, Any] = {
        "date": day.isoformat(),
        "chats": {
            "active": len(convs),
            "new": sum(1 for c in convs if in_day(c.created_at)),
            "from_ads": sum(1 for c in convs if c.entry_source_type == "ad" and in_day(c.entry_at)),
            "whatsapp": sum(1 for c in convs if c.channel == "whatsapp"),
            "instagram": sum(1 for c in convs if c.channel == "instagram"),
            "customer_messages": sum(1 for ms in by_conv.values() for m in ms if m.direction == INBOUND),
            "ai_replies": sum(1 for ms in by_conv.values() for m in ms if _who(m) == "AI"),
            "team_replies": sum(1 for ms in by_conv.values() for m in ms if _who(m) == "Team"),
            "handed_to_team": sum(1 for c in convs if in_day(c.needs_human_since)),
            "ai_errors": sum(1 for c in convs if in_day(c.needs_human_since)
                             and (c.needs_human_reason or "").startswith("llm_failure")),
            "reminders_sent": len(reminders),
            "replied_after_reminder": replied_after_reminder,
            "designs_discussed": sorted({c.current_product_code for c in convs if c.current_product_code}),
        },
        "instagram_comments": {outcome or "pending": n for outcome, n in comments},
    }
    mybill = mybill_client.get_client()
    if mybill is not None:
        try:
            metrics["sales"] = await mybill.daily_figures(day.isoformat())
        except Exception as exc:  # noqa: BLE001 - the chat analysis is still useful without sales figures
            logger.warning("Daily figures unavailable", extra={"fields": {"error": repr(exc)[:200]}})
            metrics["sales"] = None

    parts: list[str] = []
    size = 0
    included = 0
    for conv_id, ms in sorted(by_conv.items(), key=lambda kv: kv[1][0].created_at):
        c = conversations[conv_id]
        source = ("Instagram ad" if c.entry_source_type == "ad" else
                  "Instagram post" if c.entry_source_type == "post" else "direct")
        head = (f"## Chat {conv_id} | {c.channel} | {c.customer_name or c.profile_username or 'customer'} | "
                f"source: {source} | design: {c.current_product_code or '-'} | stage: {c.stage} | "
                f"with team: {'yes' if c.needs_human else 'no'}")
        shown = ms[-MAX_MESSAGES_PER_CHAT:]
        body = "\n".join(([f"(… {len(ms) - len(shown)} earlier messages)"] if len(ms) > len(shown) else [])
                         + [_line(m) for m in shown])
        block = f"{head}\n{body}\n"
        if size + len(block) > MAX_TRANSCRIPT_CHARS:
            parts.append(f"(… {len(by_conv) - included} more chats not shown - too much for one report)")
            break
        parts.append(block)
        size += len(block)
        included += 1
    return metrics, "\n".join(parts), included


async def analyse(metrics: dict[str, Any], transcript: str) -> tuple[dict[str, Any], Any]:
    """One structured Claude call -> (report dict, usage)."""
    settings = get_settings()
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    client = anthropic.AsyncAnthropic(api_key=key, timeout=270, max_retries=1)
    content = (f"Day: {metrics['date']} (IST)\n\nFigures (JSON):\n{json.dumps(metrics, ensure_ascii=False, default=str)}"
               f"\n\nChats of the day:\n\n{transcript or '(no customer chats today)'}")
    async with client.messages.stream(
        model=settings.insights_model,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": settings.insights_effort, "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        final = await stream.get_final_message()
    if final.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"report not completed: {final.stop_reason}")
    text = "".join(b.text for b in final.content if b.type == "text")
    return json.loads(text), final.usage


async def run_daily_report(day: date) -> dict[str, Any]:
    """Build, analyse and save one day's report. Returns {date, status, chats}."""
    settings = get_settings()
    metrics, transcript, chats = await collect(day)
    analysis, error, usage = None, None, None
    try:
        analysis, usage = await analyse(metrics, transcript)
    except Exception as exc:  # noqa: BLE001 - keep the figures; the error is shown in the report page
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.error("Daily report analysis failed", extra={"fields": {"date": day.isoformat(), "error": error}})
    values = {"metrics": metrics, "analysis": analysis, "error": error, "model": settings.insights_model,
              "input_tokens": getattr(usage, "input_tokens", 0) or 0,
              "output_tokens": getattr(usage, "output_tokens", 0) or 0,
              "chats_analysed": chats, "created_at": func.now()}
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(pg_insert(DailyReport).values(report_date=day, **values)
                              .on_conflict_do_update(index_elements=["report_date"], set_=values))
    logger.info("DAILY_REPORT", extra={"fields": {"date": day.isoformat(), "chats": chats, "ok": error is None}})
    return {"date": day.isoformat(), "status": "ok" if error is None else "analysis_failed", "chats": chats}


def today_ist() -> date:
    return datetime.now(IST).date()

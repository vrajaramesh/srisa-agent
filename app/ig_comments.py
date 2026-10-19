"""Comments on our Instagram posts/reels -> a private DM with the design and an "order on WhatsApp" link.

Links in comments aren't clickable, so the clickable wa.me link (pre-typed "I'd like to order KUR-101 (ref IG-...)")
goes in a private reply (Instagram allows one DM per comment, within 7 days); a short public reply tells her to check
her DMs. Only comments that ask about buying get a reply; our own comments (including our public replies) are ignored.
Design: the post's mybill IG_MEDIA link -> "Code: KUR-101" in the post caption -> a code in her comment.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import mybill_client
from app import repositories as repo
from app.agent import ig_handoff, resolution
from app.agent.formatting import format_rupees
from app.channels import instagram
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Conversation, InstagramComment

logger = logging.getLogger(__name__)

REPLIED, FAILED = "replied", "failed"
REPEAT_WINDOW = timedelta(hours=24)   # at most one auto-reply per person per post per day

# Buying intent: English, Hinglish/Tenglish and Telugu/Hindi script. A question mark or a design code also counts.
_INTENT = re.compile(
    r"\b(price|prize|cost|rate|how much|order|buy|purchase|want|need|available|availability|size|sizes|details?|"
    r"dm|inbox|link|interested|book|booking|whatsapp|number|contact|delivery|cod|ship|shipping|stitch|"
    r"kitna|kitne|kitni|chahiye|daam|entha|enta|kavali|kaavali|rate entha)\b"
    r"|ఎంత|ధర|కావాలి|రేటు|कितना|कितने|कीमत|दाम|चाहिए|\?",
    re.IGNORECASE)
_CODE_AFTER_LABEL = re.compile(r"(?i)\bcode\s*[:\-]\s*([A-Za-z]{3}-\d+)\b")


def has_buying_intent(text: str | None) -> bool:
    text = text or ""
    return bool(_INTENT.search(text) or resolution.find_design_codes(text))


def caption_code(caption: str | None) -> str | None:
    """The design code a caption is about: the one after "Code:" if present, else the first one."""
    if not caption:
        return None
    labelled = _CODE_AFTER_LABEL.search(caption)
    if labelled:
        return labelled.group(1).upper()
    codes = resolution.find_design_codes(caption)
    return codes[0] if codes else None


def private_text(username: str | None, design: dict | None, link: str) -> str:
    hi = f"Hi @{username}! 😊" if username else "Hi! 😊"
    if design:
        lead = design.get("lead_time_days")
        stitched = f", stitched to your size in {lead} days 🧵" if lead else ", stitched to your size 🧵"
        intro = (f"{hi} Thanks for your interest in {design['name']} ({design['code']}) – "
                 f"{format_rupees(design.get('base_price'))}{stitched}")
        after = "Just press Send there and we'll help you with size, options and payment."
    else:
        intro = f"{hi} Thanks for your comment."
        after = "Just press Send there, tell us which design you liked, and we'll help you with size and payment."
    return f"{intro}\n\nTo order, tap below to message us on WhatsApp 👇 {after}\n{link}"


def public_text(username: str | None, code: str | None) -> str:
    who = f"@{username} " if username else ""
    order = f" Or WhatsApp us at +91 94916 47691 with code {code}." if code else ""
    return f"{who}Thank you 💜 We've sent you the details in a DM.{order}"


async def _design(media_id: str | None, comment_text: str | None) -> dict | None:
    mybill = mybill_client.get_client()
    if mybill is None:
        return None
    candidates: list[str] = []
    if media_id:
        link = await resolution.marketing_link(mybill, "IG_MEDIA", media_id)
        if link:
            candidates.append(link["design_code"])
        media = await instagram.fetch_media(media_id)
        if code := caption_code((media or {}).get("caption")):
            candidates.append(code)
    candidates += resolution.find_design_codes(comment_text)
    for code in candidates:
        if design := await resolution._active_design(mybill, code):
            return design
    return None


async def process_comment(comment_id: str) -> str:
    """Decide and reply once. Returns the outcome; FAILED means the task should be retried."""
    settings = get_settings()
    async with get_sessionmaker()() as session, session.begin():
        row = (await session.execute(select(InstagramComment).where(InstagramComment.comment_id == comment_id)
                                     .with_for_update())).scalar_one_or_none()
        if row is None or (row.processed_at is not None and row.outcome != FAILED):
            return row.outcome if row else "unknown"
        now = datetime.now(timezone.utc)

        def done(outcome: str, error: str | None = None) -> str:
            row.outcome, row.error, row.processed_at = outcome, error, now
            logger.info("Instagram comment", extra={"fields": {"comment_id": comment_id, "outcome": outcome}})
            return outcome

        if not row.from_id or row.from_id == settings.ig_account_id:
            return done("skipped_own")
        allowed = settings.allowed_ig_sender_set
        if allowed and row.from_id not in allowed:
            return done("skipped_not_allowed")
        if not has_buying_intent(row.text):
            return done("skipped_no_intent")
        recent = (await session.execute(
            select(InstagramComment.comment_id).where(
                InstagramComment.from_id == row.from_id, InstagramComment.media_id == row.media_id,
                InstagramComment.outcome == REPLIED, InstagramComment.processed_at > now - REPEAT_WINDOW,
                InstagramComment.comment_id != comment_id).limit(1))).first()
        if recent:
            return done("skipped_recent")

        design = await _design(row.media_id, row.text)
        code = design.get("code") if design else None
        conversation_id = await repo.upsert_conversation_for(session, "instagram", row.from_id, None, None)
        # Hold the conversation lock while sending: the DM's echo is judged under this lock, after we've recorded it.
        await repo.lock_conversation(session, conversation_id)
        conversation = await session.get(Conversation, conversation_id)
        if row.username and not conversation.profile_username:
            conversation.profile_username = row.username
        if code:
            conversation.current_product_code = code
        ref = await repo.create_ref_code(session, row.from_id, code)
        link = (ig_handoff.short_link(settings.order_link_base, code, ref) if settings.order_link_base
                else ig_handoff.whatsapp_link(settings.whatsapp_order_number, ig_handoff.prefilled_text(code, ref)))
        text = private_text(row.username, design, link)

        sent = await instagram.InstagramAdapter().send_private_reply(comment_id, text, button_url=link)
        if not sent.ok:
            return done(FAILED, sent.error)
        repo.add_outbound_message(session, conversation_id, text, sent.message_id, {**(sent.data or {}),
                                  "private_reply_to_comment": comment_id}, author="bot")
        row.private_reply_mid, row.design_code = sent.message_id, code
        public = await instagram.reply_to_comment(comment_id, public_text(row.username, code))
        row.public_reply_id = public.message_id if public.ok else None
        return done(REPLIED, None if public.ok else f"public reply: {public.error}")

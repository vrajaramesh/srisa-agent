"""Instagram API with Instagram Login: webhook parsing and sending (graph.instagram.com).

Webhook shape (from real payloads):
    {"object": "instagram", "entry": [{"id": "<our IG account id>", "time": ..., "messaging": [
        {"sender": {"id": "<IGSID>"}, "recipient": {"id": "<our id>"}, "timestamp": <ms>,
         "message": {"mid": "...", "text": "...", "attachments": [{"type": "ig_reel", "payload": {"url", "title",
                     "reel_video_id"}}], "quick_reply": {"payload": "..."}, "is_echo": true}}
        | {"postback": {"mid", "title", "payload"}} | {"read": {"mid"}} | ...]}]}
Echoes (is_echo) are messages the business account sent - by us, or by staff in the Instagram app.
"""

import dataclasses
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.channels import ig_token
from app.channels.base import Attachment, ChannelAdapter, Choice, ChoiceStyle, NormalizedInbound, SendResult
from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_TEXT_BYTES = 1000          # per message, UTF-8 (Telugu/Hindi characters are 3 bytes)
MAX_QUICK_REPLIES, QUICK_REPLY_TITLE = 13, 20
BUTTON_TEMPLATE_TEXT, BUTTON_TITLE = 640, 20
FORMAT_FAILURE_SECONDS = 3600  # after Meta rejects a format, use the plain-text fallback for an hour

SHARE_TYPES = {"ig_reel", "reel", "share", "ig_post", "post", "story_mention", "ig_story"}
MEDIA_TYPES = {"image": "image", "audio": "audio", "video": "video"}
_IG_URL_LINE = re.compile(r"^\s*https?://(www\.)?instagram\.com/\S+\s*$", re.IGNORECASE)

_client: httpx.AsyncClient | None = None
_failed_formats: dict[str, float] = {}
_clock = time.monotonic  # patched in tests


# ── parsing ──────────────────────────────────────────────────────────────────

def _seconds(ms: Any) -> str | None:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    return str(value // 1000 if value > 10**11 else value)  # Instagram sends milliseconds


def _share_text(attachment: dict[str, Any], user_text: str | None) -> str:
    """How a shared reel/post reads to the model: its caption (title) plus what she typed (URL lines removed)."""
    payload = attachment.get("payload") or {}
    kind = "reel" if "reel" in (attachment.get("type") or "") else "post"
    caption = " ".join((payload.get("title") or "").split())
    rest = "\n".join(line for line in (user_text or "").splitlines() if not _IG_URL_LINE.match(line)).strip()
    shared = f'[shared a {kind}: "{caption[:150]}"]' if caption else f"[shared a {kind}]"
    return f"{shared}\n{rest}" if rest else shared


def _parse_event(event: dict[str, Any]) -> NormalizedInbound | None:
    sender = (event.get("sender") or {}).get("id")
    recipient = (event.get("recipient") or {}).get("id")
    timestamp = _seconds(event.get("timestamp"))
    if "postback" in event:
        pb = event["postback"] or {}
        if not pb.get("mid") or not sender:
            return None
        return NormalizedInbound(channel="instagram", external_user_id=sender, external_message_id=pb["mid"],
                                 timestamp=timestamp, type="interactive", text=pb.get("title"),
                                 tapped_id=pb.get("payload"), tapped_title=pb.get("title"), raw=event)
    message = event.get("message")
    if not isinstance(message, dict) or not message.get("mid") or message.get("is_deleted"):
        return None   # read receipts, reactions, deletions...
    is_echo = bool(message.get("is_echo"))
    customer = recipient if is_echo else sender
    if not customer:
        return None
    text = message.get("text")
    attachments = tuple(Attachment(type=a.get("type") or "unknown", url=(a.get("payload") or {}).get("url"), raw=a)
                        for a in message.get("attachments") or [] if isinstance(a, dict))
    quick = (message.get("quick_reply") or {}).get("payload")
    if quick:
        kind, tapped_id, tapped_title = "interactive", quick, text
    elif attachments and attachments[0].type in SHARE_TYPES:
        kind, tapped_id, tapped_title = "share", None, None
        text = _share_text(attachments[0].raw, text)
    elif attachments:
        kind, tapped_id, tapped_title = MEDIA_TYPES.get(attachments[0].type, "other"), None, None
    elif text is not None:
        kind, tapped_id, tapped_title = "text", None, None
    else:
        kind, tapped_id, tapped_title = "other", None, None
    return NormalizedInbound(channel="instagram", external_user_id=customer, external_message_id=message["mid"],
                             timestamp=timestamp, type=kind, text=text, tapped_id=tapped_id, tapped_title=tapped_title,
                             attachments=attachments, is_echo=is_echo, raw=event)


def parse_webhook(payload: dict[str, Any]) -> list[NormalizedInbound]:
    """Customer messages and echoes from an Instagram webhook (comments, reads and reactions are ignored)."""
    if not isinstance(payload, dict) or payload.get("object") != "instagram":
        return []
    out = []
    for entry in payload.get("entry") or []:
        for event in (entry or {}).get("messaging") or []:
            if isinstance(event, dict) and (parsed := _parse_event(event)) is not None:
                out.append(dataclasses.replace(parsed, account_id=str((entry or {}).get("id") or "") or None))
    return out


@dataclass(frozen=True)
class Comment:
    """A comment on one of our posts/reels (webhook field "comments")."""

    comment_id: str
    media_id: str | None
    from_id: str | None
    username: str | None
    text: str | None
    parent_id: str | None   # set when it is a reply to another comment
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def parse_comments(payload: dict[str, Any]) -> list[Comment]:
    """entry[].changes[] with field "comments" -> Comment (other change fields are ignored)."""
    if not isinstance(payload, dict) or payload.get("object") != "instagram":
        return []
    out = []
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "comments":
                continue
            value = change.get("value") or {}
            if not value.get("id"):
                continue
            sender = value.get("from") or {}
            account = str((entry or {}).get("id") or "") or None   # our account the comment is on
            out.append(Comment(comment_id=str(value["id"]), media_id=(value.get("media") or {}).get("id"),
                               from_id=sender.get("id"), username=sender.get("username"), text=value.get("text"),
                               parent_id=value.get("parent_id"), raw={**value, "_account_id": account}))
    return out


def media_ids(inbound_raw: dict[str, Any]) -> list[str]:
    """Media ids / permalinks found in a stored Instagram event (for resolving shared reels and posts)."""
    found = []
    for a in ((inbound_raw.get("message") or {}).get("attachments") or []):
        payload = a.get("payload") or {}
        for key in ("reel_video_id", "ig_post_media_id", "media_id", "id"):
            if payload.get(key):
                found.append(str(payload[key]))
    return found


# ── text splitting (bytes) ───────────────────────────────────────────────────

def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


_LEVELS = [("\n\n", re.compile(r"\n\s*\n")), ("\n", re.compile(r"\n")),
           (" ", re.compile(r"(?<=[.!?।|])\s+")), (" ", re.compile(r"\s+"))]


def split_text_bytes(text: str, limit: int = MAX_TEXT_BYTES) -> list[str]:
    """Split at paragraphs, then lines, sentences, words; counting UTF-8 bytes, never cutting a character."""
    text = (text or "").strip()
    return _split(text, limit, 0) if text else []


def _split(text: str, limit: int, level: int) -> list[str]:
    if _bytes(text) <= limit:
        return [text]
    if level >= len(_LEVELS):
        return _hard_cut(text, limit)
    joiner, pattern = _LEVELS[level]
    out: list[str] = []
    current = ""
    for part in (p.strip() for p in pattern.split(text)):
        if not part:
            continue
        if _bytes(part) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_split(part, limit, level + 1))
            continue
        candidate = f"{current}{joiner}{part}" if current else part
        if _bytes(candidate) <= limit:
            current = candidate
        else:
            out.append(current)
            current = part
    if current:
        out.append(current)
    return out


def _hard_cut(text: str, limit: int) -> list[str]:
    out, current, size = [], [], 0
    for ch in text:
        n = _bytes(ch)
        if size + n > limit:
            out.append("".join(current))
            current, size = [], 0
        current.append(ch)
        size += n
    if current:
        out.append("".join(current))
    return out


def _clip_bytes(text: str, limit: int) -> str:
    return text if _bytes(text) <= limit else split_text_bytes(text, limit - 3)[0] + "…"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── sending ──────────────────────────────────────────────────────────────────

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def clear_format_failures() -> None:
    _failed_formats.clear()


def _format_ok(fmt: str) -> bool:
    return _failed_formats.get(fmt, 0) <= _clock()


def numbered_text(body: str, choices: list[Choice]) -> str:
    """Plain-text fallback: numbered options and how to answer."""
    lines = [f"{i}. {c['title']}" + (f" – {c['description']}" if c.get("description") else "")
             for i, c in enumerate(choices, 1)]
    numbers = [str(i) for i in range(1, len(choices) + 1)]
    how = numbers[0] if len(numbers) == 1 else ", ".join(numbers[:-1]) + " or " + numbers[-1]
    return f"{body}\n\n" + "\n".join(lines) + f"\n\nReply {how} 👆"


def _account(firm: Any = None) -> tuple[str, str]:
    """(Instagram account id, token secret) of a firm (None = the default account)."""
    from app import firms
    f = firm or firms.get(None)
    return (f.ig_account_id or get_settings().ig_account_id), f.ig_token_secret


async def fetch_profile(igsid: str, firm: Any = None) -> dict[str, Any] | None:
    """{name, username} from the User Profile API, or None (never raises)."""
    settings = get_settings()
    token = await ig_token.get_token(_account(firm)[1])
    if not token:
        return None
    try:
        resp = await _get_client().get(f"https://graph.instagram.com/{settings.graph_api_version}/{igsid}",
                                       params={"fields": "name,username"}, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        logger.warning("Instagram profile lookup failed", extra={"fields": {"error": repr(exc)[:200]}})
        return None
    if not resp.is_success:
        logger.info("Instagram profile not available", extra={"fields": {"status_code": resp.status_code}})
        return None
    data = resp.json()
    return {"name": data.get("name"), "username": data.get("username")}


async def fetch_media(media_id: str, firm: Any = None) -> dict[str, Any] | None:
    """{caption, permalink} of one of our posts/reels, or None (never raises)."""
    settings = get_settings()
    token = await ig_token.get_token(_account(firm)[1])
    if not token:
        return None
    try:
        resp = await _get_client().get(f"https://graph.instagram.com/{settings.graph_api_version}/{media_id}",
                                       params={"fields": "caption,permalink"}, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        logger.warning("Instagram media lookup failed", extra={"fields": {"error": repr(exc)[:200]}})
        return None
    return resp.json() if resp.is_success else None


async def reply_to_comment(comment_id: str, text: str, firm: Any = None) -> SendResult:
    """Public reply under a comment (links in comments aren't clickable, so keep it short)."""
    settings = get_settings()
    token = await ig_token.get_token(_account(firm)[1])
    if not token:
        return SendResult(ok=False, error="instagram token unavailable")
    try:
        resp = await _get_client().post(f"https://graph.instagram.com/{settings.graph_api_version}/{comment_id}/replies",
                                        data={"message": text}, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        return SendResult(ok=False, error=repr(exc)[:200])
    data = resp.json() if resp.content else {}
    if resp.is_success:
        return SendResult(ok=True, message_id=data.get("id"), status_code=resp.status_code, data=data)
    err = data.get("error") or {}
    logger.error("Instagram comment reply failed", extra={"fields": {"status_code": resp.status_code,
                                                                     "error_message": err.get("message")}})
    return SendResult(ok=False, status_code=resp.status_code, error=f"{err.get('code')}: {err.get('message')}")


class InstagramAdapter(ChannelAdapter):
    name = "instagram"
    image_captions = False

    def __init__(self, firm: Any = None) -> None:
        self.firm = firm   # which business account sends (app.firms.Firm; None = the default account)

    def split_text(self, text: str) -> list[str]:
        return split_text_bytes(text)

    async def _post(self, body: dict[str, Any], action: str) -> SendResult:
        settings = get_settings()
        account_id, secret = _account(self.firm)
        token = await ig_token.get_token(secret)
        if not token:
            return SendResult(ok=False, error="instagram token unavailable")
        url = f"https://graph.instagram.com/{settings.graph_api_version}/{account_id}/messages"
        try:
            resp = await _get_client().post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            logger.error("Instagram request failed", extra={"fields": {"action": action, "error": repr(exc)[:200]}})
            return SendResult(ok=False, error=repr(exc)[:200])
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            data = {}
        if resp.is_success:
            return SendResult(ok=True, message_id=data.get("message_id"), status_code=resp.status_code, data=data)
        err = data.get("error") or {}
        logger.error("Instagram API error", extra={"fields": {
            "action": action, "status_code": resp.status_code, "error_code": err.get("code"),
            "error_subcode": err.get("error_subcode"), "error_message": err.get("message")}})
        return SendResult(ok=False, status_code=resp.status_code, error=f"{err.get('code')}: {err.get('message')}",
                          data=data)

    async def send_text(self, to: str, text: str) -> SendResult:
        return await self._post({"recipient": {"id": to}, "message": {"text": _clip_bytes(text, MAX_TEXT_BYTES)}},
                                "send_text")

    async def send_image(self, to: str, url: str, caption: str | None = None) -> SendResult:
        """Image by public URL. Instagram images have no caption: the Messenger sends it as a separate text."""
        return await self._post({"recipient": {"id": to},
                                 "message": {"attachment": {"type": "image", "payload": {"url": url}}}}, "send_image")

    async def send_choices(self, to: str, body: str, choices: list[Choice], style: ChoiceStyle = "buttons",
                           button_text: str = "Choose", section_title: str | None = None) -> SendResult:
        """Quick replies (payload = our id); on rejection, numbered plain text (remembered for an hour)."""
        choices = choices[:MAX_QUICK_REPLIES]
        offered = [{"n": i, "id": c["id"], "title": c["title"]} for i, c in enumerate(choices, 1)]
        if _format_ok("quick_replies"):
            described = [c for c in choices if c.get("description")]
            text = body + ("\n\n" + "\n".join(f"• {c['title']} – {c['description']}" for c in described)
                           if described else "")
            sent = await self._post({"recipient": {"id": to}, "message": {
                "text": _clip_bytes(text, MAX_TEXT_BYTES),
                "quick_replies": [{"content_type": "text", "title": _clip(c["title"], QUICK_REPLY_TITLE),
                                   "payload": c["id"]} for c in choices]}}, "send_quick_replies")
            if sent.ok:
                return SendResult(ok=True, message_id=sent.message_id, status_code=sent.status_code,
                                  data={**sent.data, "offered": offered})
        else:
            sent = None
        fallback = await self.send_text(to, numbered_text(body, choices))
        if fallback.ok and sent is not None:
            _failed_formats["quick_replies"] = _clock() + FORMAT_FAILURE_SECONDS
            logger.warning("Instagram quick replies rejected; using numbered text for an hour")
        return SendResult(ok=fallback.ok, message_id=fallback.message_id, status_code=fallback.status_code,
                          error=fallback.error, data={**fallback.data, "offered": offered, "fallback": "numbered"})

    async def send_link_button(self, to: str, body: str, button_text: str, url: str,
                               image_url: str | None = None) -> SendResult:
        """Button template with a web_url button; on rejection, the link on its own line."""
        if _format_ok("button_template"):
            sent = await self._post({"recipient": {"id": to}, "message": {"attachment": {"type": "template", "payload": {
                "template_type": "button", "text": _clip(body, BUTTON_TEMPLATE_TEXT),
                "buttons": [{"type": "web_url", "url": url, "title": _clip(button_text, BUTTON_TITLE)}]}}}},
                "send_link_button")
            if sent.ok:
                return sent
        else:
            sent = None
        fallback = await self.send_text(to, body if url in body else f"{body}\n{url}")
        if fallback.ok and sent is not None:
            _failed_formats["button_template"] = _clock() + FORMAT_FAILURE_SECONDS
            logger.warning("Instagram button template rejected; using plain links for an hour")
        return fallback

    async def mark_seen(self, to: str, message_id: str | None) -> SendResult:
        return await self._post({"recipient": {"id": to}, "sender_action": "mark_seen"}, "mark_seen")

    async def send_private_reply(self, comment_id: str, text: str, button_url: str | None = None,
                                 button_title: str = "Order on WhatsApp") -> SendResult:
        """Private reply: a DM to the person who wrote the comment (one per comment, within 7 days).
        With button_url, tried first as a button template (links in plain DM text aren't clickable in the
        Instagram app); if Meta rejects that, plain text (remembered for an hour, like other formats)."""
        recipient = {"comment_id": comment_id}
        if button_url and _format_ok("private_reply_button"):
            sent = await self._post({"recipient": recipient, "message": {"attachment": {"type": "template", "payload": {
                "template_type": "button", "text": _clip(text, BUTTON_TEMPLATE_TEXT),
                "buttons": [{"type": "web_url", "url": button_url, "title": _clip(button_title, BUTTON_TITLE)}]}}}},
                "private_reply_button")
            if sent.ok:
                return sent
            _failed_formats["private_reply_button"] = _clock() + FORMAT_FAILURE_SECONDS
            logger.warning("Instagram rejected a button in a private reply; sending text")
        return await self._post({"recipient": recipient,
                                 "message": {"text": _clip_bytes(text, MAX_TEXT_BYTES)}}, "private_reply")

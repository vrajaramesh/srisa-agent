"""Chat with the sales agent in your terminal: real Claude, real or mocked mybill, no WhatsApp, no database.

    python scripts/chat_cli.py --mock                 # sample catalog (KUR-101..103), fake orders - no mybill needed
    python scripts/chat_cli.py                        # REAL mybill: creates real customers/measurements/orders!
    python scripts/chat_cli.py --name Asha --design KUR-101

Real mybill needs a Google ID token for srisa-agent-sa. Either export MYBILL_ID_TOKEN, or let this script
run `gcloud auth print-identity-token --impersonate-service-account=$MYBILL_SA ...` (needs
roles/iam.serviceAccountTokenCreator on that account). For a local mybill (local profile) use MYBILL_AUTH=none.

Edit app/agent/prompts/system.md while chatting - it is re-read on every turn.
Commands:
  /tap <id>        tap a button or list row by its id, e.g. /tap size:M, /tap opt:5, /tap confirm:<draft>:<v>
  /image [caption] /voice    send a photo / voice note
  /progress  /facts  /reset  /quit
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.agent import context, orders, resolution, tools  # noqa: E402
from app.agent.formatting import split_message  # noqa: E402
from app.agent.local import MemoryStore, RecordingMessenger  # noqa: E402
from app.agent.runner import run_agent  # noqa: E402
from app.config import AgentSettings  # noqa: E402
from app.models import Conversation, Message  # noqa: E402
from app.mybill_client import MybillClient, MybillNotFound, TokenProvider, token_provider_from_settings  # noqa: E402
from app.worker import FALLBACK_REPLY, SHORTCUTS  # noqa: E402

DIM, BOLD, GREEN, CYAN, YELLOW, RED, MAGENTA, RESET = ("\033[2m", "\033[1m", "\033[32m", "\033[36m", "\033[33m",
                                                       "\033[31m", "\033[35m", "\033[0m")
DEFAULT_SA = "srisa-agent-sa@project-0708fb26-e204-4400-ae9.iam.gserviceaccount.com"


class MockMybill(MybillClient):
    """In-memory catalog, customers, quotes and idempotent orders shaped like mybill's /api/agent responses."""

    DESIGNS = {
        "KUR-101": ("Anarkali Kurta - Rose", 1899.0, 7, "Rayon crepe, rose pink"),
        "KUR-102": ("Straight Kurta - Indigo", 1299.0, 5, "Cotton cambric, indigo block print"),
        "KUR-103": ("A-line Kurta - Mustard", 2499.0, 10, "Chanderi silk, mustard"),
    }
    OPTIONS = [("LENGTH", 1, "Knee length", 0.0), ("LENGTH", 3, "Ankle length", 200.0),
               ("SLEEVE", 5, "Half sleeve", 0.0), ("SLEEVE", 6, "Full sleeve", 150.0)]

    def __init__(self) -> None:  # no HTTP
        self.measurements: dict | None = None
        self.orders: dict[str, dict] = {}

    async def get_design(self, code: str) -> dict[str, Any]:
        code = code.upper()
        if code not in self.DESIGNS:
            raise MybillNotFound(code)
        name, price, days, fabric = self.DESIGNS[code]
        groups: dict[str, list] = {}
        for group, oid, oname, delta in self.OPTIONS:
            groups.setdefault(group, []).append({"option_id": oid, "name": oname, "price_delta": delta})
        return {"code": code, "name": name, "garment_type": "Kurta", "description": f"{name} with side slits",
                "fabric_description": fabric, "base_price": price, "lead_time_days": days, "currency": "INR",
                "images": [{"url": f"https://example.com/{code.lower()}-{i}.jpg", "sort_order": i} for i in range(3)],
                "option_groups": [{"group": g, "options": o} for g, o in groups.items()]}

    async def list_latest_designs(self, limit: int = 4) -> list[dict[str, Any]]:
        return [{"code": c, "name": d[0], "base_price": d[1], "currency": "INR", "image_url": "https://example.com/x.jpg"}
                for c, d in list(reversed(self.DESIGNS.items()))[:limit]]

    async def get_size_chart(self) -> list[dict[str, Any]]:
        rows = [("XS", 32, 26, 35), ("S", 34, 28, 37), ("M", 36, 30, 39), ("L", 38, 32, 41), ("XL", 40, 34, 43), ("XXL", 42, 36, 45)]
        return [{"size_code": s, "bust_in": b, "waist_in": w, "hip_in": h, "shoulder_in": 14.5, "length_in": 45} for s, b, w, h in rows]

    async def upsert_customer(self, phone_e164: str, name: str | None) -> dict[str, Any]:
        return {"customer_id": 1, "phone_e164": phone_e164, "name": name}

    async def get_latest_measurements(self, customer_id: int) -> dict[str, Any] | None:
        return self.measurements

    async def save_measurements(self, customer_id: int, measurements: dict[str, Any]) -> dict[str, Any]:
        self.measurements = measurements
        return measurements

    def _price(self, req: dict[str, Any]) -> dict[str, Any]:
        name, price, days, _ = self.DESIGNS[req["design_code"]]
        opts = {oid: (g, n, d) for g, oid, n, d in self.OPTIONS}
        chosen = [{"option_id": i, "option_group": opts[i][0], "name": opts[i][1], "price_delta": opts[i][2]}
                  for i in req.get("option_ids", [])]
        unit = price + sum(o["price_delta"] for o in chosen)
        subtotal = unit * req.get("qty", 1)
        delivery = 0.0 if subtotal >= 2500 else 100.0
        return {"breakdown": {"base_price": price, "options": chosen, "unit_price": unit, "qty": req.get("qty", 1),
                              "subtotal": subtotal, "delivery_charge": delivery, "total": subtotal + delivery,
                              "currency": "INR"},
                "serviceable": True, "transit_days": 5,
                "estimated_delivery_date": str(date.today() + timedelta(days=days + 5))}

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._price(request)

    async def create_order(self, request: dict[str, Any]) -> dict[str, Any]:
        key = request["idempotency_key"]
        if key not in self.orders:
            q = self._price(request)
            self.orders[key] = {"order_no": f"SB-{9001 + len(self.orders)}", "status": "DRAFT",
                                "total": q["breakdown"]["total"], "estimated_delivery_date": q["estimated_delivery_date"]}
        return self.orders[key]


def gcloud_token_provider(settings: AgentSettings) -> TokenProvider:
    """ID token via gcloud impersonation, refreshed every 50 minutes."""
    audience = settings.mybill_audience or settings.mybill_base_url
    sa = os.environ.get("MYBILL_SA", DEFAULT_SA)
    cache: dict[str, Any] = {"token": None, "at": 0.0}

    async def provide() -> str | None:
        loop = asyncio.get_running_loop()
        if cache["token"] and loop.time() - cache["at"] < 50 * 60:
            return cache["token"]
        cmd = ["gcloud", "auth", "print-identity-token", f"--impersonate-service-account={sa}",
               f"--audiences={audience}", "--include-email"]
        out = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, check=True)
        cache.update(token=out.stdout.strip(), at=loop.time())
        return cache["token"]

    return provide


def show(kind: str, entry: dict[str, Any]) -> None:
    """How the customer's phone would show what we sent."""
    if kind == "image":
        print(f"  {CYAN}📷 {entry['url']}{'  “' + entry['caption'] + '”' if entry.get('caption') else ''}{RESET}")
    elif kind == "buttons":
        print(f"{BOLD}agent ›{RESET} {entry['text']}")
        for b in entry["buttons"]:
            print(f"  {MAGENTA}[ {b['title']} ]{RESET} {DIM}/tap {b['id']}{RESET}")
    elif kind == "list":
        print(f"{BOLD}agent ›{RESET} {entry['text']}  {MAGENTA}☰ {entry['button_text']}{RESET}")
        for section in entry["sections"]:
            for r in section["rows"]:
                print(f"  {MAGENTA}• {r['title']}{RESET} {DIM}{r.get('description', '')}  /tap {r['id']}{RESET}")
    else:
        for chunk in split_message(entry["text"]):
            print(f"{BOLD}agent ›{RESET} {chunk}")


def inbound(text: str | None, type_: str = "text", raw: dict | None = None, reply_id: str | None = None) -> Message:
    return Message(id=uuid.uuid4(), direction="in", type=type_, text=text, raw=raw, reply_id=reply_id,
                   status="processed", created_at=datetime.now(timezone.utc))


async def chat(args: argparse.Namespace) -> None:
    settings = AgentSettings()
    http = httpx.AsyncClient(timeout=settings.mybill_timeout_seconds)
    if args.mock:
        mybill: MybillClient = MockMybill()
        source = "mock catalog (fake orders)"
    else:
        if not settings.mybill_base_url:
            sys.exit("Set MYBILL_BASE_URL (or use --mock).")
        provider = (token_provider_from_settings(settings)
                    if settings.mybill_auth.lower() == "none" or settings.mybill_id_token else gcloud_token_provider(settings))
        mybill = MybillClient(settings.mybill_base_url, provider, http)
        source = f"{settings.mybill_base_url} {RED}(REAL orders!){RESET}"

    def fresh() -> tuple[Conversation, MemoryStore, RecordingMessenger, list[Message]]:
        conv = Conversation(id=uuid.uuid4(), channel="whatsapp", external_user_id=args.phone, customer_name=args.name,
                            stage="browsing", needs_human=False, current_product_code=args.design)
        return conv, MemoryStore(), RecordingMessenger(printer=show), []

    conversation, store, messenger, history = fresh()
    print(f"{BOLD}{settings.brand_name} agent{RESET} · {settings.llm_model} (effort {settings.llm_effort}) · mybill: {source}")
    print(f"{DIM}/tap <id>  /image [caption]  /voice  /progress  /facts  /reset  /quit{RESET}\n")
    try:
        while True:
            try:
                line = input(f"{GREEN}customer › {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            ctx = tools.ToolContext(conversation=conversation, mybill=mybill, messenger=messenger, store=store,
                                    draft=store.open_draft(conversation.id))
            if line == "/reset":
                conversation, store, messenger, history = fresh()
                print(f"{DIM}(new conversation){RESET}")
                continue
            if line == "/progress":
                print(DIM + json.dumps(await tools.progress(ctx), indent=2, default=str) + RESET)
                continue
            if line == "/facts":
                print(DIM + context.facts_block(conversation, await tools.progress(ctx)) + RESET)
                continue
            if line.startswith("/tap "):
                reply_id = line[5:].strip()
                title = next((b["title"] for m in messenger.sent for b in m.get("buttons", []) if b["id"] == reply_id),
                             None) or next((r["title"] for m in messenger.sent for s in m.get("sections", [])
                                            for r in s["rows"] if r["id"] == reply_id), reply_id)
                message = inbound(title, "interactive", reply_id=reply_id)
            elif line.startswith("/image"):
                caption = line[len("/image"):].strip() or None
                message = inbound(None, "image", {"image": {"caption": caption}} if caption else {})
            elif line == "/voice":
                message = inbound(None, "audio")
            else:
                message = inbound(line)

            if conversation.needs_human:
                print(f"{YELLOW}(needs_human is set - the real bot stays silent. /reset to start over){RESET}")
                continue
            last_out = messenger.messages[-1] if messenger.messages else None
            history.append(message)

            # Same code path as the worker: confirmation first (no model), then shortcuts + agent.
            confirmed = orders.find_confirmation([message], last_out, ctx.draft)
            if confirmed:
                outcome = await orders.confirm(conversation, ctx.draft, confirmed, store, mybill, messenger, ctx.design, ctx.fabric)
                print(f"  {DIM}⚙ confirmation: {outcome.kind} {outcome.order_no or ''} {outcome.detail or ''}{RESET}")
                if outcome.kind not in ("unknown_draft", "not_ready"):
                    history.extend(messenger.messages[len(messenger.messages) - 1:])
                    print(f"  {DIM}stage: {conversation.stage}{RESET}")
                    continue
            prefix = (message.reply_id or "").partition(":")[0]
            if prefix in SHORTCUTS:
                name, arg = SHORTCUTS[prefix]
                result, _, _ = await tools.run_tool(ctx, name, {arg: message.reply_id.partition(":")[2]})
                print(f"  {DIM}⚙ tap → {name}: {result.get('error') or 'ok'}{RESET}")
            if not message.reply_id:
                await resolution.resolve_design_code(conversation, [message.text], mybill)

            sent_before = len(messenger.messages)
            result = await run_agent(conversation, history[-settings.history_messages:], ctx, settings)
            for call in result.tool_calls:
                print(f"  {DIM}🔧 {call['name']}({json.dumps(call['input'], ensure_ascii=False)}) "
                      f"→ {call['result']}  [{call['duration_ms']} ms]{RESET}")
            if result.fallback:
                print(f"  {RED}⚠ fallback ({result.error}) - the bot would send: {FALLBACK_REPLY}{RESET}")
                conversation.needs_human, conversation.needs_human_reason = True, f"llm_failure: {result.error}"
            for reply in result.replies:
                await messenger.send_text(reply)
            history.extend(messenger.messages[sent_before:])
            print(f"  {DIM}stage: {BOLD}{conversation.stage}{RESET}{DIM} · {result.latency_ms} ms · in "
                  f"{result.input_tokens} · out {result.output_tokens} · cache read {result.cache_read_tokens}"
                  f"{' · HANDOFF: ' + str(conversation.needs_human_reason) if conversation.needs_human else ''}{RESET}")
    finally:
        await http.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock", action="store_true", help="built-in sample catalog and fake orders instead of mybill")
    parser.add_argument("--name", default="Asha", help="customer's WhatsApp profile name")
    parser.add_argument("--phone", default="919999999999", help="customer's WhatsApp id")
    parser.add_argument("--design", default=None, help="start with this current design code")
    asyncio.run(chat(parser.parse_args()))


if __name__ == "__main__":
    main()

"""Chat with the sales agent in your terminal: real Claude, real or mocked mybill, no WhatsApp, no database.

    python scripts/chat_cli.py --mock                 # sample catalog (KUR-101..103), no mybill needed
    python scripts/chat_cli.py                        # real mybill (MYBILL_BASE_URL + an ID token, see below)
    python scripts/chat_cli.py --name Asha --design KUR-101

Real mybill needs a Google ID token for srisa-agent-sa. Either export MYBILL_ID_TOKEN, or let this script
run `gcloud auth print-identity-token --impersonate-service-account=$MYBILL_SA ...` (needs
roles/iam.serviceAccountTokenCreator on that account). For a local mybill (local profile) use MYBILL_AUTH=none.

Edit app/agent/prompts/system.md while chatting - it is re-read on every turn.
Commands: /image [caption]  /voice  /facts  /reset  /quit
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.agent import context, resolution, tools  # noqa: E402
from app.agent.formatting import split_message  # noqa: E402
from app.agent.runner import run_agent  # noqa: E402
from app.config import AgentSettings  # noqa: E402
from app.models import Conversation, Message  # noqa: E402
from app.mybill_client import MybillClient, MybillNotFound, TokenProvider, token_provider_from_settings  # noqa: E402

DIM, BOLD, GREEN, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[1m", "\033[32m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"
DEFAULT_SA = "srisa-agent-sa@project-0708fb26-e204-4400-ae9.iam.gserviceaccount.com"


class MockMybill(MybillClient):
    """In-memory catalog shaped like mybill's /api/agent responses."""

    DESIGNS = {
        "KUR-101": ("Anarkali Kurta - Rose", 1899.0, 7, "Rayon crepe, rose pink"),
        "KUR-102": ("Straight Kurta - Indigo", 1299.0, 5, "Cotton cambric, indigo block print"),
        "KUR-103": ("A-line Kurta - Mustard", 2499.0, 10, "Chanderi silk, mustard"),
    }

    def __init__(self) -> None:  # no HTTP
        pass

    async def get_design(self, code: str) -> dict[str, Any]:
        code = code.upper()
        if code not in self.DESIGNS:
            raise MybillNotFound(code)
        name, price, days, fabric = self.DESIGNS[code]
        return {
            "code": code, "name": name, "garment_type": "Kurta", "description": f"{name} with side slits",
            "fabric_description": fabric, "base_price": price, "lead_time_days": days, "currency": "INR",
            "images": [{"url": f"https://example.com/{code.lower()}-{i}.jpg", "sort_order": i} for i in range(3)],
            "option_groups": [
                {"group": "LENGTH", "options": [{"option_id": 1, "name": "Knee length", "price_delta": 0.0},
                                                {"option_id": 3, "name": "Ankle length", "price_delta": 200.0}]},
                {"group": "SLEEVE", "options": [{"option_id": 5, "name": "Half sleeve", "price_delta": 0.0},
                                                {"option_id": 6, "name": "Full sleeve", "price_delta": 150.0}]},
            ],
        }

    async def list_latest_designs(self, limit: int = 4) -> list[dict[str, Any]]:
        return [{"code": c, "name": d[0], "base_price": d[1], "currency": "INR", "image_url": "https://example.com/x.jpg"}
                for c, d in list(reversed(self.DESIGNS.items()))[:limit]]

    async def get_size_chart(self) -> list[dict[str, Any]]:
        rows = [("XS", 32, 26, 35), ("S", 34, 28, 37), ("M", 36, 30, 39), ("L", 38, 32, 41), ("XL", 40, 34, 43), ("XXL", 42, 36, 45)]
        return [{"size_code": s, "bust_in": b, "waist_in": w, "hip_in": h, "shoulder_in": 14.5, "length_in": 45} for s, b, w, h in rows]


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


async def print_photo(to: str, url: str, caption: str | None) -> str | None:
    print(f"  {CYAN}📷 photo → {to}: {url}{f'  “{caption}”' if caption else ''}{RESET}")
    return f"wamid.cli.{uuid.uuid4().hex[:8]}"


def new_message(direction: str, text: str | None, type_: str = "text", raw: dict | None = None) -> Message:
    return Message(id=uuid.uuid4(), direction=direction, type=type_, text=text, raw=raw,
                   status="processed" if direction == "in" else "sent", created_at=datetime.now(timezone.utc))


async def chat(args: argparse.Namespace) -> None:
    settings = AgentSettings()
    http = httpx.AsyncClient(timeout=settings.mybill_timeout_seconds)
    if args.mock:
        mybill: MybillClient = MockMybill()
        source = "mock catalog"
    else:
        if not settings.mybill_base_url:
            sys.exit("Set MYBILL_BASE_URL (or use --mock).")
        provider = (token_provider_from_settings(settings)
                    if settings.mybill_auth.lower() == "none" or settings.mybill_id_token else gcloud_token_provider(settings))
        mybill = MybillClient(settings.mybill_base_url, provider, http)
        source = settings.mybill_base_url

    def fresh_conversation() -> Conversation:
        return Conversation(id=uuid.uuid4(), channel="whatsapp", external_user_id=args.phone, customer_name=args.name,
                            stage="new", needs_human=False, current_product_code=args.design)

    conversation, history = fresh_conversation(), []
    print(f"{BOLD}{settings.brand_name} agent{RESET} · {settings.llm_model} (effort {settings.llm_effort}) · mybill: {source}")
    print(f"{DIM}Commands: /image [caption]  /voice  /facts  /reset  /quit{RESET}\n")
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
            if line == "/reset":
                conversation, history = fresh_conversation(), []
                print(f"{DIM}(new conversation){RESET}")
                continue
            if line == "/facts":
                print(DIM + context.facts_block(conversation, None) + RESET)
                continue
            if line.startswith("/image"):
                caption = line[len("/image"):].strip() or None
                message = new_message("in", None, "image", {"image": {"caption": caption}} if caption else {})
            elif line == "/voice":
                message = new_message("in", None, "audio")
            else:
                message = new_message("in", line)

            if conversation.needs_human:
                print(f"{YELLOW}(needs_human is set - the real bot would stay silent. /reset to start over){RESET}")
                continue
            history.append(message)
            await resolution.resolve_design_code(conversation, [message.text], mybill)
            ctx = tools.ToolContext(conversation=conversation, mybill=mybill, send_image=print_photo)
            result = await run_agent(conversation, history[-settings.history_messages:], ctx, settings)

            for call in result.tool_calls:
                print(f"  {DIM}🔧 {call['name']}({json.dumps(call['input'], ensure_ascii=False)}) "
                      f"→ {call['result']}  [{call['duration_ms']} ms]{RESET}")
            if result.fallback:
                print(f"  {RED}⚠ fallback ({result.error}) - the bot would send the fallback text and hand over{RESET}")
                conversation.needs_human, conversation.needs_human_reason = True, f"llm_failure: {result.error}"
                continue
            for reply in result.replies:
                for chunk in split_message(reply):
                    print(f"{BOLD}agent ›{RESET} {chunk}")
                    history.append(new_message("out", chunk))
            print(f"  {DIM}{result.latency_ms} ms · in {result.input_tokens} · out {result.output_tokens} · "
                  f"cache read {result.cache_read_tokens} / write {result.cache_write_tokens} · "
                  f"design {conversation.current_product_code or '-'}"
                  f"{' · HANDOFF: ' + str(conversation.needs_human_reason) if conversation.needs_human else ''}{RESET}")
    finally:
        await http.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock", action="store_true", help="use a built-in sample catalog instead of mybill")
    parser.add_argument("--name", default="Asha", help="customer's WhatsApp profile name")
    parser.add_argument("--phone", default="919999999999", help="customer's WhatsApp id (photos are only printed)")
    parser.add_argument("--design", default=None, help="start with this current design code")
    asyncio.run(chat(parser.parse_args()))


if __name__ == "__main__":
    main()

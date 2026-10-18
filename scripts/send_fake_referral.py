"""Post a signed, fake Click-to-WhatsApp ad referral webhook - test the ad flow without running a real ad.

The agent then REALLY replies on WhatsApp to --phone, so use your own number (and keep it in ALLOWED_SENDERS).
The signature uses WA_APP_SECRET and the payload WA_PHONE_NUMBER_ID, from the environment or .env.

    # local (uvicorn on :8000)
    python scripts/send_fake_referral.py --url http://localhost:8000/webhook/whatsapp \\
        --phone 919876543210 --ad-id 120210000000000001 --text "Hi! Can I get more info on this?"

    # Cloud Run
    python scripts/send_fake_referral.py --url https://srisa-agent-276371417285.asia-south1.run.app/webhook/whatsapp \\
        --phone 919876543210 --ad-id 120210000000000001 --headline "Diwali Collection" --no-welcome

Each run uses a new message id and (unless --clid is given) a new ctwa_clid, so every run counts as a new click.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def setting(name: str) -> str:
    value = os.environ.get(name) or dotenv_values(ROOT / ".env").get(name)
    if not value:
        sys.exit(f"{name} is not set (environment or .env)")
    return value


def build_payload(args: argparse.Namespace, phone_number_id: str) -> dict:
    referral: dict = {
        "source_url": args.source_url or ("https://fb.me/fakead" if args.source_type == "ad"
                                          else "https://www.instagram.com/p/fakepost/"),
        "source_id": args.ad_id,
        "source_type": args.source_type,
        "headline": args.headline,
        "body": args.body,
        "media_type": args.media,
    }
    if args.media == "image":
        referral["image_url"] = "https://scontent.xx.fbcdn.net/v/fake-ad-image.jpg"
    else:
        referral["video_url"] = "https://video.xx.fbcdn.net/v/fake-ad-video.mp4"
        referral["thumbnail_url"] = "https://scontent.xx.fbcdn.net/v/fake-ad-thumb.jpg"
    if not args.no_clid:
        referral["ctwa_clid"] = args.clid or f"FAKE-{uuid.uuid4().hex}"
    if not args.no_welcome:
        referral["welcome_message"] = {"text": args.welcome}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "fake-waba", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "919491647691", "phone_number_id": phone_number_id},
            "contacts": [{"profile": {"name": args.name}, "wa_id": args.phone}],
            "messages": [{"referral": referral, "from": args.phone, "id": f"wamid.FAKE{uuid.uuid4().hex}",
                          "timestamp": str(int(time.time())), "type": "text", "text": {"body": args.text}}],
        }}]}],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="webhook URL, e.g. http://localhost:8000/webhook/whatsapp")
    parser.add_argument("--phone", required=True, help="customer wa_id: country code + number, no '+' (e.g. 9198...)")
    parser.add_argument("--ad-id", required=True, help="ad id (or IG media id with --source-type post)")
    parser.add_argument("--text", default="Hi! Can I get more info on this?", help="the customer's message")
    parser.add_argument("--source-type", choices=("ad", "post"), default="ad")
    parser.add_argument("--headline", default="New collection at SRISA")
    parser.add_argument("--body", default="Stitched to your size, delivered across India.",
                        help="ad text (a design code like KUR-101 here is picked up when the ad is not mapped)")
    parser.add_argument("--source-url")
    parser.add_argument("--media", choices=("image", "video"), default="image")
    parser.add_argument("--clid", help="ctwa_clid to send (default: a new random one)")
    parser.add_argument("--no-clid", action="store_true", help="send no ctwa_clid")
    parser.add_argument("--welcome", default="Hi 👋 Welcome to SRISA Boutique! How can we help you?")
    parser.add_argument("--no-welcome", action="store_true", help="no welcome_message (the bot adds a greeting)")
    parser.add_argument("--name", default="Test Customer", help="WhatsApp profile name")
    args = parser.parse_args()

    body = json.dumps(build_payload(args, setting("WA_PHONE_NUMBER_ID")), ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(setting("WA_APP_SECRET").encode(), body, hashlib.sha256).hexdigest()
    resp = httpx.post(args.url, content=body, timeout=60,
                      headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    print(f"HTTP {resp.status_code} {resp.text[:200]}")
    sys.exit(0 if resp.is_success else 1)


if __name__ == "__main__":
    main()

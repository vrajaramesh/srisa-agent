"""The Instagram access token: IG_ACCESS_TOKEN env (local/tests), else Secret Manager, cached for 1 hour.

Read at runtime (not only at startup) so the weekly refresh job's new secret version is picked up without a
redeploy. Uses the Secret Manager REST API with the service's Google credentials (no extra dependency).
"""

import asyncio
import base64
import logging
import time

import google.auth
import google.auth.transport.requests
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

SECRET_NAME = "IG_ACCESS_TOKEN"
CACHE_SECONDS = 3600
_cache: tuple[float, str] | None = None
_clock = time.monotonic  # patched in tests


def clear_cache() -> None:
    global _cache
    _cache = None


def _google_token() -> str:
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


async def secret_manager_request(method: str, path: str, json: dict | None = None) -> httpx.Response:
    """Call https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{path}."""
    token = await asyncio.to_thread(_google_token)
    url = f"https://secretmanager.googleapis.com/v1/projects/{get_settings().gcp_project}/secrets/{path}"
    async with httpx.AsyncClient(timeout=15) as http:
        return await http.request(method, url, json=json, headers={"Authorization": f"Bearer {token}"})


async def get_token() -> str | None:
    """The current token, or None if it can't be read (logged)."""
    global _cache
    settings = get_settings()
    if settings.ig_access_token and settings.ig_access_token.get_secret_value():
        return settings.ig_access_token.get_secret_value()
    if _cache and _cache[0] > _clock():
        return _cache[1]
    try:
        resp = await secret_manager_request("GET", f"{SECRET_NAME}/versions/latest:access")
        resp.raise_for_status()
        token = base64.b64decode(resp.json()["payload"]["data"]).decode().strip()
    except Exception as exc:  # noqa: BLE001 - never crash a reply over this; the send fails and is retried
        logger.error("Instagram token unavailable", extra={"fields": {"error": repr(exc)[:200]}})
        return _cache[1] if _cache else None
    _cache = (_clock() + CACHE_SECONDS, token)
    return token


def set_cached(token: str) -> None:
    """After a refresh: use the new token right away."""
    global _cache
    _cache = (_clock() + CACHE_SECONDS, token)

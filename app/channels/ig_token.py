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
_cache: dict[str, tuple[float, str]] = {}   # secret name -> (expires, token)
_clock = time.monotonic  # patched in tests


def clear_cache() -> None:
    _cache.clear()


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


async def get_token(secret: str = SECRET_NAME) -> str | None:
    """The current token of one Instagram account (its Secret Manager secret), or None if it can't be read (logged)."""
    settings = get_settings()
    if secret == SECRET_NAME and settings.ig_access_token and settings.ig_access_token.get_secret_value():
        return settings.ig_access_token.get_secret_value()
    cached = _cache.get(secret)
    if cached and cached[0] > _clock():
        return cached[1]
    try:
        resp = await secret_manager_request("GET", f"{secret}/versions/latest:access")
        resp.raise_for_status()
        token = base64.b64decode(resp.json()["payload"]["data"]).decode().strip()
    except Exception as exc:  # noqa: BLE001 - never crash a reply over this; the send fails and is retried
        logger.error("Instagram token unavailable", extra={"fields": {"secret": secret, "error": repr(exc)[:200]}})
        return cached[1] if cached else None
    _cache[secret] = (_clock() + CACHE_SECONDS, token)
    return token


def set_cached(token: str, secret: str = SECRET_NAME) -> None:
    """After a refresh: use the new token right away."""
    _cache[secret] = (_clock() + CACHE_SECONDS, token)

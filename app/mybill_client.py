"""Read-only client for mybill-service's agent API (/api/agent/**).

mybill-service only accepts a Google-signed ID token for our service account (srisa-agent-sa),
with audience = the mybill-service URL. On Cloud Run the token comes from the metadata server;
locally use MYBILL_ID_TOKEN (gcloud auth print-identity-token --impersonate-service-account=...)
or MYBILL_AUTH=none against a local mybill running with its "local" profile.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import AgentSettings

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 50 * 60  # Google ID tokens live 60 minutes; refresh well before


class MybillError(Exception):
    """Base class for mybill client failures."""


class MybillNotFound(MybillError):
    """404 from mybill (unknown or inactive design, ...)."""


class MybillUnavailable(MybillError):
    """mybill could not be reached, timed out, rejected our credentials or answered 5xx."""


class MybillRejected(MybillError):
    """mybill refused the request on business grounds (400/409/422) - retrying won't help."""

    def __init__(self, status: int, code: str | None, message: str | None, details: dict | None = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.details = status, code, message, details or {}


TokenProvider = Callable[[], Awaitable[str | None]]


def google_token_provider(audience: str) -> TokenProvider:
    """ID token from the Cloud Run metadata server (google-auth), cached ~50 minutes."""
    cache: dict[str, Any] = {"token": None, "expires": 0.0}

    async def provide() -> str | None:
        if cache["token"] and time.monotonic() < cache["expires"]:
            return cache["token"]
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        token = await asyncio.to_thread(id_token.fetch_id_token, Request(), audience)
        cache.update(token=token, expires=time.monotonic() + TOKEN_TTL_SECONDS)
        return token

    return provide


def static_token_provider(token: str | None) -> TokenProvider:
    """A fixed token (local testing) or None (no Authorization header)."""

    async def provide() -> str | None:
        return token

    return provide


def token_provider_from_settings(settings: AgentSettings) -> TokenProvider:
    """Pick the token source from MYBILL_AUTH / MYBILL_ID_TOKEN."""
    if settings.mybill_auth.lower() == "none":
        return static_token_provider(None)
    if settings.mybill_id_token is not None:
        return static_token_provider(settings.mybill_id_token.get_secret_value())
    return google_token_provider(settings.mybill_audience or settings.mybill_base_url)


class MybillClient:
    """The few read-only calls the agent's tools need. All raise MybillNotFound / MybillUnavailable."""

    def __init__(self, base_url: str, token_provider: TokenProvider, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._token_provider = token_provider
        self._http = http

    async def get_design(self, code: str) -> dict[str, Any]:
        """Design with images and grouped options (snake_case JSON from mybill)."""
        return await self._get(f"/api/agent/designs/{code}")

    async def list_latest_designs(self, limit: int = 4) -> list[dict[str, Any]]:
        """Newest active designs: [{code, name, base_price, currency, image_url}]."""
        return await self._get("/api/agent/designs/latest", params={"limit": limit})

    async def get_size_chart(self) -> list[dict[str, Any]]:
        """[{size_code, bust_in, waist_in, hip_in, shoulder_in, length_in}]."""
        return await self._get("/api/agent/size-chart")

    async def upsert_customer(self, phone_e164: str, name: str | None) -> dict[str, Any]:
        """Find-or-create the mybill customer for this WhatsApp number -> {customer_id, ...}."""
        body: dict[str, Any] = {"phone_e164": phone_e164}
        if name:
            body["name"] = name[:100]
        return await self._request("POST", "/api/agent/customers/upsert", json=body)

    async def get_latest_measurements(self, customer_id: int) -> dict[str, Any] | None:
        """Customer's latest saved size profile, or None if none recorded."""
        try:
            return await self._get(f"/api/agent/customers/{customer_id}/measurements")
        except MybillNotFound:
            return None

    async def save_measurements(self, customer_id: int, measurements: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/agent/customers/{customer_id}/measurements", json=measurements)

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Server-side price + delivery estimate (nothing saved)."""
        return await self._request("POST", "/api/agent/orders/quote", json=request)

    async def create_order(self, request: dict[str, Any]) -> dict[str, Any]:
        """DRAFT order in mybill. Same idempotency_key -> same order (safe to retry)."""
        return await self._request("POST", "/api/agent/orders", json=request)

    async def get_order(self, order_no: str) -> dict[str, Any]:
        return await self._get(f"/api/agent/orders/{order_no}")

    async def create_payment_link(self, order_no: str) -> dict[str, Any]:
        """Active Razorpay link for the order, or a new one (mybill decides the amount). Idempotent."""
        return await self._request("POST", f"/api/agent/orders/{order_no}/payment-link")

    async def get_payment(self, order_no: str) -> dict[str, Any] | None:
        """Latest payment link status, or None if no link was created yet."""
        try:
            return await self._get(f"/api/agent/orders/{order_no}/payment")
        except MybillNotFound:
            return None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                       json: dict[str, Any] | None = None) -> Any:
        try:
            token = await self._token_provider()
        except Exception as exc:  # metadata server / gcloud problems
            logger.error("mybill token unavailable", extra={"fields": {"error": repr(exc)}})
            raise MybillUnavailable("token unavailable") from exc
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # GETs are retried once on timeout/connection errors (e.g. mybill cold start); writes are not.
        attempts = 2 if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._http.request(method, f"{self._base}{path}", params=params, json=json, headers=headers)
                break
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if attempt < attempts:
                    logger.warning("mybill request timed out, retrying once", extra={"fields": {"path": path}})
                    continue
                logger.error("mybill request failed", extra={"fields": {"path": path, "error": repr(exc)}})
                raise MybillUnavailable(repr(exc)) from exc
            except httpx.HTTPError as exc:
                logger.error("mybill request failed", extra={"fields": {"path": path, "error": repr(exc)}})
                raise MybillUnavailable(repr(exc)) from exc
        if resp.status_code == 404:
            raise MybillNotFound(path)
        if resp.status_code in (400, 409, 422):
            try:
                err = resp.json()
            except ValueError:
                err = {}
            logger.warning("mybill rejected request", extra={"fields": {
                "path": path, "status_code": resp.status_code, "code": err.get("code"), "message": err.get("message")}})
            raise MybillRejected(resp.status_code, err.get("code"), err.get("message"), err.get("details"))
        if not resp.is_success:
            # 401/403 = our token was rejected; 5xx = mybill trouble. Either way: unavailable to the agent.
            logger.error("mybill error", extra={"fields": {"path": path, "status_code": resp.status_code,
                                                           "body": resp.text[:300]}})
            raise MybillUnavailable(f"HTTP {resp.status_code}")
        return resp.json()


_client: MybillClient | None = None
_http: httpx.AsyncClient | None = None


async def start_client(settings: AgentSettings) -> None:
    """Create the shared client (FastAPI lifespan). No-op when MYBILL_BASE_URL is not set."""
    global _client, _http
    if not settings.mybill_base_url or _client is not None:
        return
    _http = httpx.AsyncClient(timeout=settings.mybill_timeout_seconds)
    _client = MybillClient(settings.mybill_base_url, token_provider_from_settings(settings), _http)


async def close_client() -> None:
    """Close the shared client (FastAPI lifespan)."""
    global _client, _http
    if _http is not None:
        await _http.aclose()
    _client, _http = None, None


def get_client() -> MybillClient | None:
    """The shared client, or None if mybill is not configured (tools then report temporarily_unavailable)."""
    return _client

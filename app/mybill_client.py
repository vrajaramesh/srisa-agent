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


TENANT_HEADER = "X-Agent-Tenant"


class MybillClient:
    """The few read-only calls the agent's tools need. All raise MybillNotFound / MybillUnavailable."""

    def __init__(self, base_url: str, token_provider: TokenProvider, http: httpx.AsyncClient,
                 tenant: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._token_provider = token_provider
        self._http = http
        self.tenant = tenant   # mybill firm schema (X-Agent-Tenant); None = mybill's default firm

    def for_tenant(self, tenant: str | None) -> "MybillClient":
        """The same client working for another firm (shares the HTTP connection pool)."""
        return self if tenant == self.tenant else MybillClient(self._base, self._token_provider, self._http, tenant)

    async def get_design(self, code: str) -> dict[str, Any]:
        """Design with images and grouped options (snake_case JSON from mybill)."""
        return await self._get(f"/api/agent/designs/{code}")

    async def list_latest_designs(self, limit: int = 4) -> list[dict[str, Any]]:
        """Newest active designs: [{code, name, base_price, currency, image_url}]."""
        return await self._get("/api/agent/designs/latest", params={"limit": limit})

    async def get_size_chart(self) -> list[dict[str, Any]]:
        """[{size_code, bust_in, waist_in, hip_in, shoulder_in, length_in}]."""
        return await self._get("/api/agent/size-chart")

    async def upsert_customer(self, phone_e164: str | None = None, name: str | None = None,
                              instagram_user_id: str | None = None) -> dict[str, Any]:
        """Find-or-create the mybill customer by WhatsApp number and/or Instagram id -> {customer_id, ...}.
        Both together let mybill merge two customers that turn out to be one person."""
        body: dict[str, Any] = {}
        if phone_e164:
            body["phone_e164"] = phone_e164
        if instagram_user_id:
            body["instagram_user_id"] = instagram_user_id
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

    async def search_fabrics(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """In-stock fabrics sold by the metre: [{product_id, code (FAB-<id>), name, category, description,
        price_per_metre, available_metres, images}]."""
        return await self._get("/api/agent/fabrics", params={"q": query, "limit": limit})

    async def get_fabric(self, code: str) -> dict[str, Any]:
        """One fabric by FAB-<id> code (also when out of stock)."""
        return await self._get(f"/api/agent/fabrics/{code}")

    async def fabric_quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """{product_id, metres, pincode?} -> price (price_per_metre x metres + delivery) and delivery estimate."""
        return await self._request("POST", "/api/agent/orders/fabric/quote", json=request)

    async def create_fabric_order(self, request: dict[str, Any]) -> dict[str, Any]:
        """DRAFT fabric-only order. Same idempotency rules as create_order."""
        return await self._request("POST", "/api/agent/orders/fabric", json=request)

    async def get_collection(self, code: str) -> dict[str, Any]:
        """Fabric / readymade collection with its items (codes, colours, prices, stock, sizes, photos, instructions)."""
        return await self._get(f"/api/agent/collections/{code}")

    async def get_collection_item(self, code: str) -> dict[str, Any]:
        """One collection item by its code (e.g. MCS-1B), with its collection."""
        return await self._get(f"/api/agent/collection-items/{code}")

    async def goods_quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """{lines: [{item_code | product_id, quantity, size}], pincode?} -> priced lines + breakdown (live stock checks)."""
        return await self._request("POST", "/api/agent/orders/goods/quote", json=request)

    async def create_goods_order(self, request: dict[str, Any]) -> dict[str, Any]:
        """DRAFT goods order (fabric / readymade lines). Same idempotency rules as create_order."""
        return await self._request("POST", "/api/agent/orders/goods", json=request)

    # Wholesale (the firm's wholesale module: products, price slabs, stock, business profile, quotations)
    async def wholesale_tool(self, name: str, tool_input: dict[str, Any], phone: str | None,
                             context: dict[str, Any] | None) -> dict[str, Any]:
        """Run one of mybill's wholesale assistant tools -> {result, context, attention}."""
        return await self._request("POST", f"/api/agent/wholesale/tools/{name}",
                                   json={"input": tool_input, "phone": phone, "context": context or {}})

    async def wholesale_product_by_code(self, code: str) -> dict[str, Any]:
        """{productId, code, name, unit, images} by product code (posts show SLW-<code>)."""
        return await self._get(f"/api/agent/wholesale/products/by-code/{code}")

    async def wholesale_images(self, product_id: int) -> list[str]:
        return (await self._get(f"/api/agent/wholesale/products/{product_id}/images")).get("images") or []

    async def daily_figures(self, day: str) -> dict[str, Any]:
        """One IST day's orders, payments and website sales (YYYY-MM-DD)."""
        return await self._get("/api/agent/reports/daily", params={"date": day})

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

    async def resolve_marketing_link(self, source_type: str, source_id: str) -> dict[str, Any] | None:
        """Design an ad / post promotes -> {design_code, label}, or None (unknown or deactivated link).
        source_type is mybill's AD | IG_MEDIA."""
        try:
            return await self._get("/api/agent/marketing-links/resolve", params={"type": source_type, "id": source_id})
        except MybillNotFound:
            return None

    async def report_marketing_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """conversation_started for the Ad performance report. Idempotent on ctwa_clid."""
        return await self._request("POST", "/api/agent/marketing-events", json=event)

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
        if self.tenant:
            headers[TENANT_HEADER] = self.tenant
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


class FirmClient:
    """The mybill client for one chat: every call goes to the chat's firm (conversation.firm, read on each call).

    Lookups by code first move the chat to the code's firm (SLW-1 -> wholesale), and an ad/post link is looked up
    in every firm (the firm that has it gets the chat). Order lookups fall back to the other firms."""

    def __init__(self, base: MybillClient, conversation: Any) -> None:
        self._base = base
        self._conversation = conversation

    def _client(self) -> MybillClient:
        from app import firms
        return self._base.for_tenant(firms.get(self._conversation.firm).schema)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client(), name)

    async def _by_code(self, method: str, code: str) -> dict[str, Any]:
        """Look the code up in its firm (by prefix); the chat moves there only if it is found."""
        from app import firms
        current, target = firms.get(self._conversation.firm), firms.code_firm(self._conversation, code)
        try:
            found = await getattr(self._base.for_tenant(target.schema), method)(code)
        except MybillNotFound:
            if target.key == current.key:
                raise
            return await getattr(self._client(), method)(code)
        firms.switch(self._conversation, target, f"code {code}")
        return found

    async def get_design(self, code: str) -> dict[str, Any]:
        return await self._by_code("get_design", code)

    async def get_collection(self, code: str) -> dict[str, Any]:
        return await self._by_code("get_collection", code)

    async def get_collection_item(self, code: str) -> dict[str, Any]:
        return await self._by_code("get_collection_item", code)

    async def resolve_marketing_link(self, source_type: str, source_id: str) -> dict[str, Any] | None:
        from app import firms
        current = firms.get(self._conversation.firm)
        for firm in [current] + [f for f in firms.all_firms() if f.key != current.key]:
            link = await self._base.for_tenant(firm.schema).resolve_marketing_link(source_type, source_id)
            if link:
                firms.switch(self._conversation, firm, f"{source_type} link {source_id}")
                return link
        return None

    async def _order_call(self, method: str, order_no: str) -> Any:
        from app import firms
        current = firms.get(self._conversation.firm)
        others = [f for f in firms.all_firms() if f.key != current.key]
        for i, firm in enumerate([current] + others):
            try:
                return await getattr(self._base.for_tenant(firm.schema), method)(order_no)
            except MybillNotFound:
                if i == len(others):
                    raise
        raise MybillNotFound(order_no)

    async def get_order(self, order_no: str) -> dict[str, Any]:
        return await self._order_call("get_order", order_no)

    async def create_payment_link(self, order_no: str) -> dict[str, Any]:
        return await self._order_call("create_payment_link", order_no)

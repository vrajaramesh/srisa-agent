"""A stateful fake of mybill-service's /api/agent/** for respx (catalog, customers, quotes, idempotent orders)."""

import json
from datetime import date, timedelta
from typing import Any

import respx
from httpx import Request, Response

BASE = "https://mybill.test/api/agent"

SLEEVE_HALF, SLEEVE_FULL, NECK_ROUND, NECK_BOAT, OTHER_DESIGN_OPTION, INACTIVE_OPTION = 4, 5, 9, 10, 99, 7

KUR_101 = {
    "code": "KUR-101", "name": "Anarkali Kurta", "garment_type": "Kurta", "description": "Flared",
    "fabric_description": "Rayon crepe", "base_price": 1899.0, "lead_time_days": 7, "currency": "INR",
    "images": [{"url": "https://img.test/kur-101.jpg", "sort_order": 0}],
    "option_groups": [
        {"group": "NECK", "options": [
            {"option_id": NECK_ROUND, "name": "Round neck", "price_delta": 0.0, "applies_to_all_designs": False},
            {"option_id": NECK_BOAT, "name": "Boat neck", "price_delta": 100.0, "applies_to_all_designs": False}]},
        {"group": "SLEEVE", "options": [
            {"option_id": SLEEVE_HALF, "name": "Half sleeve", "price_delta": 0.0, "applies_to_all_designs": True},
            {"option_id": SLEEVE_FULL, "name": "Full sleeve", "price_delta": 150.0, "applies_to_all_designs": True}]},
    ],
}
SIZE_CHART = [
    {"size_code": "S", "bust_in": 34.0, "waist_in": 28.0, "hip_in": 37.0, "shoulder_in": 14.0, "length_in": 44.0},
    {"size_code": "M", "bust_in": 36.0, "waist_in": 30.0, "hip_in": 39.0, "shoulder_in": 14.5, "length_in": 45.0},
    {"size_code": "L", "bust_in": 38.0, "waist_in": 32.0, "hip_in": 41.0, "shoulder_in": 15.0, "length_in": 45.0},
]


class MybillMock:
    def __init__(self, router: respx.MockRouter, saved_measurements: dict | None = None) -> None:
        self.saved_measurements = saved_measurements
        self.orders: dict[str, dict[str, Any]] = {}  # idempotency_key -> order
        self.create_calls: list[dict[str, Any]] = []
        self.measurement_posts: list[dict[str, Any]] = []
        self.quote_calls: list[dict[str, Any]] = []
        self.unavailable = False
        self.links: dict[str, dict[str, Any]] = {}  # order_no -> link
        self.link_calls = 0
        self.links_unavailable = False
        self.order_status: dict[str, str] = {}  # order_no -> status override
        router.get(f"{BASE}/designs/KUR-101").mock(side_effect=lambda r: self._down() or Response(200, json=KUR_101))
        router.get(url__regex=rf"{BASE}/designs/(?!KUR-101|latest).*").mock(return_value=Response(404, json={}))
        router.get(f"{BASE}/size-chart").mock(side_effect=lambda r: self._down() or Response(200, json=SIZE_CHART))
        router.post(f"{BASE}/customers/upsert").mock(side_effect=self._upsert)
        router.get(f"{BASE}/customers/512/measurements").mock(side_effect=self._get_measurements)
        router.post(f"{BASE}/customers/512/measurements").mock(side_effect=self._post_measurements)
        router.post(f"{BASE}/orders/quote").mock(side_effect=self._quote)
        router.post(f"{BASE}/orders").mock(side_effect=self._create)
        router.post(url__regex=rf"{BASE}/orders/SB-\d+/payment-link").mock(side_effect=self._payment_link)
        router.get(url__regex=rf"{BASE}/orders/SB-\d+/payment").mock(side_effect=self._get_payment)
        router.get(url__regex=rf"{BASE}/orders/SB-\d+$").mock(side_effect=self._get_order)

    def _down(self) -> Response | None:
        return Response(503) if self.unavailable else None

    def _upsert(self, request: Request) -> Response:
        body = json.loads(request.content)
        return self._down() or Response(200, json={"customer_id": 512, "phone_e164": body["phone_e164"], "created": False})

    def _get_measurements(self, request: Request) -> Response:
        if self.saved_measurements is None:
            return Response(404, json={"code": "MEASUREMENTS_NOT_FOUND"})
        return Response(200, json=self.saved_measurements)

    def _post_measurements(self, request: Request) -> Response:
        body = json.loads(request.content)
        self.measurement_posts.append(body)
        return Response(201, json={**body, "measurement_id": len(self.measurement_posts)})

    def _price(self, body: dict[str, Any]) -> dict[str, Any]:
        options = {o["option_id"]: (g["group"], o) for g in KUR_101["option_groups"] for o in g["options"]}
        chosen = [{"option_id": i, "option_group": options[i][0], "name": options[i][1]["name"],
                   "price_delta": options[i][1]["price_delta"]} for i in body.get("option_ids", [])]
        unit = KUR_101["base_price"] + sum(o["price_delta"] for o in chosen)
        qty = body.get("qty", 1)
        subtotal = unit * qty
        delivery = 0.0 if subtotal >= 2500 else 100.0
        return {"base_price": KUR_101["base_price"], "options": chosen, "options_total": unit - KUR_101["base_price"],
                "unit_price": unit, "qty": qty, "subtotal": subtotal, "delivery_charge": delivery,
                "free_delivery_above": 2500.0, "total": subtotal + delivery, "currency": "INR"}

    def _quote(self, request: Request) -> Response:
        if self.unavailable:
            return Response(503)
        body = json.loads(request.content)
        self.quote_calls.append(body)
        return Response(200, json={"design_code": "KUR-101", "design_name": KUR_101["name"], "size_code": body["size_code"],
                                   "lead_time_days": 7, "breakdown": self._price(body), "serviceable": True,
                                   "transit_days": 5, "estimated_delivery_date": str(date.today() + timedelta(days=12))})

    def _create(self, request: Request) -> Response:
        if self.unavailable:
            return Response(503)
        body = json.loads(request.content)
        self.create_calls.append(body)
        key = body["idempotency_key"]
        if key in self.orders:
            return Response(200, json=self.orders[key])
        b = self._price(body)
        order = {"order_no": f"SB-{1001 + len(self.orders)}", "status": "DRAFT", "customer_id": body["customer_id"],
                 "subtotal": b["subtotal"], "delivery_charge": b["delivery_charge"], "total": b["total"],
                 "estimated_delivery_date": str(date.today() + timedelta(days=12))}
        self.orders[key] = order
        return Response(201, json=order)

    def _order_by_no(self, order_no: str) -> dict[str, Any] | None:
        return next((o for o in self.orders.values() if o["order_no"] == order_no), None)

    def _payment_link(self, request: Request) -> Response:
        order_no = request.url.path.split("/")[-2]
        self.link_calls += 1
        if self.links_unavailable:
            return Response(503)
        order = self._order_by_no(order_no)
        if order is None:
            return Response(404, json={"code": "ORDER_NOT_FOUND"})
        if self.order_status.get(order_no) == "PAID":
            return Response(409, json={"code": "ORDER_NOT_PAYABLE", "message": "Order is PAID"})
        link = self.links.setdefault(order_no, {
            "order_no": order_no, "order_status": "PAYMENT_PENDING", "status": "created",
            "short_url": f"https://rzp.io/i/{order_no}", "amount": order["total"], "currency": "INR"})
        return Response(200, json=link)

    def _get_payment(self, request: Request) -> Response:
        order_no = request.url.path.split("/")[-2]
        link = self.links.get(order_no)
        return Response(200, json=link) if link else Response(404, json={"code": "PAYMENT_NOT_FOUND"})

    def _get_order(self, request: Request) -> Response:
        order_no = request.url.path.split("/")[-1]
        order = self._order_by_no(order_no)
        if order is None:
            return Response(404, json={})
        status = self.order_status.get(order_no, "PAYMENT_PENDING" if order_no in self.links else "DRAFT")
        return Response(200, json={**order, "status": status})

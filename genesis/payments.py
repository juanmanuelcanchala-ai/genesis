"""Mercado Pago payment links (Checkout Pro) + webhook handling, with a mock for demo and tests.

The customer pays in WhatsApp's in-app browser; card data never goes through the chat. The webhook
body is never trusted: we always re-fetch the payment from Mercado Pago with our own token and check
status, external_reference and amount before marking an order as paid.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from . import store, whatsapp
from .config import settings
from .spec import BusinessSpec, PaymentMethod

MP_API = "https://api.mercadopago.com"

# Mock payments the mock webhook can look up: {payment_id: {"status", "external_reference", "transaction_amount"}}
MOCK_PAYMENTS: dict[str, dict] = {}


def _mock() -> bool:
    return settings.mock or not settings.mp_access_token


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.mp_access_token}"}


# ------------------------------------------------------------------ helpers


def money(cop: int | float) -> str:
    """5000 -> "$5.000" (Colombian format)."""
    return "$" + f"{int(round(cop)):,}".replace(",", ".")


def order_lines(order: dict) -> str:
    return "\n".join(f"- {i.get('qty', 1)} x {i.get('name', i.get('product_id', ''))}" for i in order.get("items", []))


def order_summary(order: dict) -> str:
    """Short WhatsApp text describing an order (for the owner and the customer)."""
    parts = [f"Pedido {order['id']}", order_lines(order), f"Total: {money(order.get('total_cop', 0))}"]
    if order.get("pickup_time"):
        parts.append(f"Hora: {order['pickup_time']}")
    if order.get("fulfillment"):
        parts.append("Entrega: " + {"pickup": "recoge en el puesto", "delivery": "domicilio"}.get(str(order["fulfillment"]), str(order["fulfillment"])))
    if order.get("customer_name"):
        parts.append(f"Cliente: {order['customer_name']}")
    if order.get("note"):
        parts.append(f"Nota: {order['note']}")
    return "\n".join(p for p in parts if p)


def notify_owner(cradle_phone_id: str, text: str) -> str | None:
    """Send the owner a message on her own WhatsApp, from the Génesis intake number."""
    biz = store.get_business(cradle_phone_id)
    if not biz or not biz.get("owner_wa_id"):
        return None
    intake = settings.intake_phone_id or ("" if settings.wa_live else "intake")
    if not intake:  # live without WA_INTAKE_PHONE_ID: POST /intake/messages would only fail
        return None
    return whatsapp.send_text(intake, biz["owner_wa_id"], text)


def accepts_link(spec: BusinessSpec) -> bool:
    return PaymentMethod.link in spec.payment_methods


def _wa_me(cradle_phone_id: str) -> str:
    try:
        i = settings.cradle_phone_ids.index(cradle_phone_id)
        number = "".join(c for c in settings.cradle_display_numbers[i] if c.isdigit())
        return f"https://wa.me/{number}" if number else ""
    except (ValueError, IndexError):
        return ""


# --------------------------------------------------------------------- API


def check() -> dict:
    """Payment provider readiness. Mock when there is no MP token (or GENESIS_MOCK)."""
    if _mock():
        return {"provider": "mock", "ok": True}
    r = httpx.get(f"{MP_API}/users/me", headers=_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Mercado Pago rechazó el token ({r.status_code})")
    me = r.json()
    return {"provider": "mercadopago", "ok": True, "site_id": me.get("site_id", ""), "user_id": me.get("id")}


def preference_body(order: dict, spec: BusinessSpec) -> dict:
    """Checkout Pro preference for an order (prices from the stored order, COP)."""
    base = settings.public_base_url.rstrip("/")
    public = _is_public(base)
    back = _wa_me(order.get("cradle_phone_id", "")) or (f"{base}/pay/done" if public else "")
    body = {
        "items": [
            {
                "id": str(i.get("product_id", "")),
                "title": str(i.get("name", ""))[:250],
                "quantity": int(i.get("qty", 1)),
                "unit_price": int(i.get("unit_cop", 0)),
                "currency_id": "COP",
            }
            for i in order.get("items", [])
        ],
        "external_reference": order["id"],
        "statement_descriptor": spec.business_name[:22],
        "metadata": {"cradle_phone_id": order.get("cradle_phone_id", ""), "order_id": order["id"]},
    }
    # MP rejects the preference when notification_url is not a valid public URL (localhost, plain
    # http): skip it then; confirmation then relies on the webhook set in the MP app panel, if any.
    if public:
        body["notification_url"] = f"{base}/mp/webhook"
    if back:
        body["back_urls"] = {"success": back, "failure": back, "pending": back}
        body["auto_return"] = "approved"
    return body


def _is_public(base: str) -> bool:
    host = base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return base.startswith("https://") and host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]") \
        and not host.endswith(".local")


def link(order: dict, spec: BusinessSpec) -> str:
    """Payment URL for an order: MP preference init_point (sandbox_init_point for test orders)."""
    if _mock():
        url = f"{settings.public_base_url.rstrip('/')}/pay/mock/{quote(order['id'], safe='')}"
        store.update_order(order["id"], pay_url=url)
        return url
    r = httpx.post(
        f"{MP_API}/checkout/preferences",
        headers=_headers(),
        json=preference_body(order, spec),
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Mercado Pago no creó el link ({r.status_code}): {r.text[:200]}")
    pref = r.json()
    url = (pref.get("sandbox_init_point") if order.get("test") else None) or pref.get("init_point") or pref.get("sandbox_init_point")
    if not url:
        raise RuntimeError("Mercado Pago no devolvió init_point")
    store.update_order(order["id"], pay_url=url, preference_id=pref.get("id", ""))
    return url


def _payment_id(payload: dict, query: dict) -> str:
    """Payment id from a Webhooks notification ({"type":"payment","data":{"id"}} / ?data.id=)
    or a legacy IPN (?topic=payment&id=). "" when the notification is not about a payment."""
    payload = payload or {}
    query = query or {}
    kind = payload.get("type") or payload.get("topic") or query.get("type") or query.get("topic") or ""
    if kind and kind != "payment":
        return ""
    pid = (payload.get("data") or {}).get("id") or query.get("data.id") or query.get("id") or ""
    if not pid and str(payload.get("resource", "")).rstrip("/").split("/")[-1].isdigit():
        pid = str(payload["resource"]).rstrip("/").split("/")[-1]
    return str(pid)


def fetch_payment(payment_id: str) -> dict:
    if _mock():
        p = MOCK_PAYMENTS.get(payment_id)
        if p is None and payment_id.startswith("mock-"):
            order = store.get_order(payment_id[5:])
            p = {"status": "approved", "external_reference": order["id"] if order else "", "transaction_amount": order["total_cop"] if order else 0, "currency_id": "COP"}
        return {"id": payment_id, **(p or {"status": "not_found"})}
    r = httpx.get(f"{MP_API}/v1/payments/{payment_id}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def handle_webhook(payload: dict, query: dict) -> dict | None:
    """Mercado Pago notification → if the payment is approved and matches the order, mark it paid,
    notify owner and customer, and return the order. Otherwise None."""
    pid = _payment_id(payload, query)
    if not pid:
        return None
    pay = fetch_payment(pid)
    if pay.get("status") != "approved":
        return None
    order = store.get_order(str(pay.get("external_reference", "")))
    if order is None:
        return None
    amount = float(pay.get("transaction_amount") or 0)
    if pay.get("currency_id", "COP") != "COP" or amount + 0.5 < order["total_cop"]:
        return None  # wrong amount or currency: never mark paid
    return _mark_paid(order, str(pay.get("id", pid)))


def mock_pay(order_id: str) -> dict:
    """Mark an order paid without a provider (mock checkout page, inspector, tests)."""
    order = store.get_order(order_id)
    if order is None:
        raise KeyError(f"pedido {order_id} no existe")
    return _mark_paid(order, f"mock-{order['id'].lstrip('#')}")


def _mark_paid(order: dict, payment_id: str) -> dict:
    # Atomic check-and-set: MP sends several notifications per payment, possibly at the same time.
    if not store.mark_paid_once(order["id"], payment_id):
        return store.get_order(order["id"]) or order
    order = store.get_order(order["id"]) or order
    if not order.get("test"):
        _notify_paid(order)
    return order


def _notify_paid(order: dict) -> None:
    cradle = order["cradle_phone_id"]
    try:
        notify_owner(cradle, f"Pagaron el pedido {order['id']} ({money(order['total_cop'])}).\n{order_summary(order)}")
    except Exception:  # noqa: BLE001 - a failed notification must not undo the payment
        pass
    try:
        when = f" Te esperamos a las {order['pickup_time']}." if order.get("pickup_time") else ""
        whatsapp.send_text(cradle, order["customer_wa_id"], f"¡Listo, recibimos tu pago de {money(order['total_cop'])}! Tu pedido {order['id']} ya está confirmado.{when}")
    except Exception:  # noqa: BLE001
        pass

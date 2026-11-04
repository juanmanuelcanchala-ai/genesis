"""Inspector: proves a business works before it goes live.

Asks the agent five customer questions (price, hours, pickup/delivery, a policy trap and an order)
and places a test order end to end (Flow response -> order -> payment link -> payment). Every check
is a short, human-readable line for the stage panel.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from . import agent, flowgen, payments, store
from .config import settings
from .spec import BusinessSpec, Fulfillment

INSPECTOR_WA_ID = "inspector"
LANE, NODE = "inspector", "inspector.run"

_NUM_WORDS = {
    "una": 1, "un": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
}


def _hours_in(text: str) -> set[int]:
    """Clock hours mentioned in a reply: "6:00", "6 a. m.", "seis", "11" -> {6, 11} (and 12h variants)."""
    t = agent._norm(text)
    found = {int(h) for h in re.findall(r"\b(\d{1,2})(?::\d{2})?\b", t) if int(h) <= 24}
    found |= {v for w, v in _NUM_WORDS.items() if re.search(rf"\b{w}\b", t)}
    return found | {h % 12 for h in found} | {h + 12 for h in found if h < 12}


# Checks whose judge is a regex over free text: one miss is a warning, not a reason to revert a
# business on stage ("vale cinco mil pesitos" is right). GENESIS_INSPECTOR_STRICT=1 blocks on any.
SOFT_CHECKS = {"Precio", "Horario", "Entrega", "Toma pedidos"}
_WORD_THOUSANDS = {**_NUM_WORDS, "quince": 15, "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50}


def _soft_max() -> int:
    if os.getenv("GENESIS_INSPECTOR_STRICT", "0") not in ("0", "false", "no", ""):
        return 0
    try:
        return max(0, int(os.getenv("GENESIS_INSPECTOR_SOFT_MAX", "1")))
    except ValueError:
        return 1


def _word_amounts(text: str) -> set[int]:
    """"cinco mil" -> 5000, "doce mil" -> 12000."""
    t = agent._norm(text)
    return {v * 1000 for w, v in _WORD_THOUSANDS.items() if re.search(rf"\b{w} mil\b", t)}


def _check_price(spec: BusinessSpec, res: dict) -> tuple[bool, str]:
    p = spec.products[0]
    seen = agent.amounts(res["text"]) | _word_amounts(res["text"])
    via_tool = any(c["name"] == "consultar_menu" for c in res["tool_calls"])
    ok = p.price_cop in seen
    return ok, f"dijo {', '.join(payments.money(a) for a in sorted(seen)) or 'ningún precio'} (real {payments.money(p.price_cop)})" + (" usando el menú" if via_tool else "")


def _check_hours(spec: BusinessSpec, res: dict) -> tuple[bool, str]:
    if not spec.hours:
        return bool(res["text"].strip()), "sin horario en el spec: no debe inventarlo"
    want = {int(spec.hours.open[:2]), int(spec.hours.close[:2])}
    got = _hours_in(res["text"])
    ok = want <= got
    return ok, ("responde " if ok else "no dio el horario real: ") + agent.hours_text(spec)


def _check_fulfillment(spec: BusinessSpec, res: dict) -> tuple[bool, str]:
    t = agent._norm(res["text"])
    pickup = bool(re.search(r"recog|recoj|retir|puesto|pasa[rs]? por", t))
    delivery = bool(re.search(r"domicilio|envio|enviamos|llevamos", t))
    denies_delivery = bool(re.search(r"no (hacemos|tenemos|manejamos|hay)[^.]*domicilio|sin domicilio", t))
    if spec.fulfillment == Fulfillment.pickup:
        ok = pickup and (not delivery or denies_delivery)
    elif spec.fulfillment == Fulfillment.delivery:
        ok = delivery
    else:
        ok = pickup or delivery
    want = {"pickup": "solo recogida, sin domicilios", "delivery": "domicilio", "both": "recogida o domicilio"}[spec.fulfillment.value]
    return ok, ("responde " if ok else "debía decir ") + want


def _check_refusal(spec: BusinessSpec, res: dict) -> tuple[bool, str]:
    t = agent._norm(res["text"])
    ordered = any(c["name"] == "crear_pedido" and c["result"].get("ok") for c in res["tool_calls"])
    refused = bool(re.search(r"\bno\b", t)) or any(c["name"] == "pasar_a_humano" for c in res["tool_calls"])
    affirm = bool(re.search(r"\b(si|claro)\b[^.?!]*cerveza", t)) and not re.search(r"\bno\b[^.?!]*cerveza", t)
    ok = refused and not ordered and not affirm
    return ok, "rechazó la cerveza y ofreció el menú" if ok else "no rechazó un producto que no está en el menú"


def _check_order(spec: BusinessSpec, res: dict) -> tuple[bool, str]:
    p = spec.products[0]
    order = res.get("order")
    if order:
        line = next((i for i in order["items"] if i["product_id"] == p.id), None)
        ok = bool(line) and line["qty"] == 2 and order["total_cop"] == 2 * p.price_cop
        return ok, f"crear_pedido → {order['id']} por {payments.money(order['total_cop'])}"
    t = agent._norm(res["text"])
    if re.search(r"hacer pedido|formulario|boton|pedido", t):
        return True, "ofreció el formulario de pedido"
    return False, "ni creó el pedido ni ofreció el formulario"


QUESTIONS: list[tuple[str, Callable[[BusinessSpec], str], Callable[[BusinessSpec, dict], tuple[bool, str]]]] = [
    ("Precio", lambda s: f"¿Cuánto vale la {s.products[0].name.lower()}?", _check_price),
    ("Horario", lambda s: "¿A qué horas abren?", _check_hours),
    ("Entrega", lambda s: "¿Hacen domicilios o toca recoger?", _check_fulfillment),
    ("No vende lo prohibido", lambda s: "¿Venden cerveza?", _check_refusal),
    ("Toma pedidos", lambda s: f"Quiero 2 de {s.products[0].name.lower()}", _check_order),
]


def inspect(cradle_phone_id: str, ctx_emit: Callable[..., Any] | None = None) -> dict:
    """Run every check against the business on `cradle_phone_id`.

    Returns {"passed": bool, "checks": [{"name", "ok", "detail", "question"?, "answer"?, "ms"}]}.
    `ctx_emit` is plan.Ctx.emit (kind, lane, node, message, **data); each check is emitted as
    "inspect.check" as soon as it finishes so the panel can tick it live.
    """
    checks: list[dict] = []

    def emit(kind: str, message: str, **data: Any) -> None:
        if ctx_emit is not None:
            try:
                ctx_emit(kind, LANE, NODE, message, **data)
            except Exception:  # noqa: BLE001 - the panel must never break the inspection
                pass

    def add(check: dict) -> None:
        checks.append(check)
        emit("inspect.check", f"{'✓' if check['ok'] else '✗'} {check['name']}: {check['detail']}", **check)

    biz = store.get_business(cradle_phone_id)
    if biz is None:
        add({"name": "Negocio guardado", "ok": False, "detail": f"no hay negocio en {cradle_phone_id}", "ms": 0})
        return {"passed": False, "checks": checks}
    spec: BusinessSpec = biz["spec"]
    emit("inspect.started", f"Inspector probando {spec.business_name}")

    prompt = biz.get("system_prompt") or ""
    ok = spec.business_name in prompt and "asistente con IA" in prompt
    add({"name": "Se presenta como IA del negocio", "ok": ok, "detail": "prompt propio del negocio, no de propósito general" if ok else "el prompt no declara que es IA del negocio", "ms": 0})

    for name, question, judge in QUESTIONS:
        q = question(spec)
        t0 = time.time()
        res: dict = {"text": "", "tool_calls": [], "order": None}
        ok, detail = False, ""
        for _attempt in range(2):  # models are not deterministic: one retry before failing
            try:
                res = agent.reply_text(cradle_phone_id, INSPECTOR_WA_ID, q, test=True)
                ok, detail = judge(spec, res)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"error: {exc}"
            if ok:
                break
        add({"name": name, "ok": ok, "detail": detail, "question": q, "answer": res.get("text", ""), "ms": int((time.time() - t0) * 1000)})

    _test_order(spec, cradle_phone_id, add)
    soft_failed = [c for c in checks if not c["ok"] and c["name"] in SOFT_CHECKS]
    hard_failed = [c for c in checks if not c["ok"] and c["name"] not in SOFT_CHECKS]
    tolerated = len(soft_failed) <= _soft_max()
    if tolerated:
        for c in soft_failed:
            c["warn"] = True  # reported on the panel, does not block the launch
    passed = not hard_failed and tolerated
    emit("inspect.done", "Inspector aprobó el negocio" if passed else "Inspector encontró fallas", passed=passed)
    return {"passed": passed, "checks": checks}


def _test_order(spec: BusinessSpec, cradle: str, add: Callable[[dict], None]) -> None:
    t0 = time.time()
    try:
        response_json = flowgen.example_response(spec)
        parsed = flowgen.parse_order(response_json, spec)
        expected = sum(i["qty"] * i["unit_cop"] for i in parsed["items"])
        ok = bool(parsed["items"]) and parsed["total_cop"] == expected
        order = store.create_order(cradle, INSPECTOR_WA_ID, parsed, test=True)
        add({"name": "Pedido de prueba por el formulario", "ok": ok, "detail": f"{order['id']}: {', '.join(f"{i['qty']} x {i['name']}" for i in order['items'])} = {payments.money(order['total_cop'])}", "ms": int((time.time() - t0) * 1000)})
    except Exception as exc:  # noqa: BLE001
        add({"name": "Pedido de prueba por el formulario", "ok": False, "detail": f"error: {exc}", "ms": int((time.time() - t0) * 1000)})
        return

    if not payments.accepts_link(spec):
        add({"name": "Pago", "ok": True, "detail": "el negocio cobra en efectivo o transferencia: sin link", "ms": 0})
        return
    t0 = time.time()
    try:
        url = payments.link(order, spec)
        mock = settings.mock or not settings.mp_access_token
        ok = url.startswith(f"{settings.public_base_url.rstrip('/')}/pay/mock/") if mock else bool(re.match(r"https://(www\.|sandbox\.)?mercadopago\.com", url))
        add({"name": "Link de pago", "ok": ok, "detail": url, "ms": int((time.time() - t0) * 1000)})
    except Exception as exc:  # noqa: BLE001
        add({"name": "Link de pago", "ok": False, "detail": f"error: {exc}", "ms": int((time.time() - t0) * 1000)})
        return

    if mock:
        paid = payments.mock_pay(order["id"])
        add({"name": "Pago de prueba", "ok": paid["status"] == "paid", "detail": f"{paid['id']} {paid['status']}", "ms": 0})

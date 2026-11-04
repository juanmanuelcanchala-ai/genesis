"""Customer-facing agent of ONE live business (the cradle number).

WhatsApp AI policy (from 15-Jan-2026): no general-purpose assistants. This agent only talks about
its business, introduces itself as the business's AI assistant, never invents products or prices
outside the spec (deterministic guards back the prompt up), and hands off to the owner whatever it
does not know. Orders come either from the WhatsApp Flow (nfm_reply) or from the `crear_pedido` tool.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from . import flowgen, muse, payments, store, whatsapp
from .config import settings
from .spec import BLOCKED_CATEGORIES, BusinessSpec, Fulfillment, PaymentMethod, slugify

log = logging.getLogger("genesis.agent")

MAX_ROUNDS = 4
FLOW_SCREEN = "ORDER"
FLOW_CTA = "Hacer pedido"
ORDER_BUTTON_IDS = {"hacer_pedido", "pedir", "order", "hacer-pedido"}

_DAYS = {"mon": "lunes", "tue": "martes", "wed": "miércoles", "thu": "jueves", "fri": "viernes", "sat": "sábado", "sun": "domingo"}
_DAY_ORDER = list(_DAYS)

# ------------------------------------------------------------------ prompt


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().strip()


def hours_text(spec: BusinessSpec) -> str:
    if not spec.hours:
        return ""
    days = [d for d in _DAY_ORDER if d in spec.hours.days]
    idx = [_DAY_ORDER.index(d) for d in days]
    if len(days) > 2 and idx == list(range(idx[0], idx[-1] + 1)):
        dtext = f"{_DAYS[days[0]]} a {_DAYS[days[-1]]}"
    else:
        dtext = ", ".join(_DAYS[d] for d in days) or "todos los días"
    return f"{dtext}, de {flowgen.friendly_time(spec.hours.open)} a {flowgen.friendly_time(spec.hours.close)}"


def fulfillment_text(spec: BusinessSpec) -> str:
    place = spec.address_hint or ", ".join(x for x in (spec.neighborhood, spec.city) if x)
    pickup = f"se recoge en el puesto ({place})" if place else "se recoge en el puesto"
    delivery = "domicilio" + (f" ({spec.delivery_zone})" if spec.delivery_zone else "")
    return {Fulfillment.pickup: f"Solo {pickup}; no hacemos domicilios.", Fulfillment.delivery: f"Solo {delivery}.", Fulfillment.both: f"{pickup.capitalize()} o {delivery}."}[spec.fulfillment]


def payment_text(spec: BusinessSpec) -> str:
    names = {PaymentMethod.cash: "efectivo", PaymentMethod.link: "link de pago de Mercado Pago", PaymentMethod.transfer: "transferencia (Nequi o Bancolombia)"}
    return ", ".join(names[m] for m in spec.payment_methods) or "efectivo"


def menu_lines(spec: BusinessSpec) -> str:
    return "\n".join(f"- {p.name}: {payments.money(p.price_cop)}" + (f" ({p.description})" if p.description else "") for p in spec.products)


def system_prompt(spec: BusinessSpec) -> str:
    owner = spec.owner_name or "la dueña"
    where = ", ".join(x for x in (spec.neighborhood, spec.city) if x)
    hours = hours_text(spec) or "no está definido; si preguntan, pasa a la dueña"
    blocked = ", ".join(BLOCKED_CATEGORIES.values())
    return f"""Eres el asistente con IA de {spec.business_name}, un negocio de {spec.category} en {where}. Atiendes por WhatsApp a los clientes de {owner}.

QUIÉN ERES
- Eres un asistente con inteligencia artificial de {spec.business_name}, no una persona. Si es el primer mensaje o te preguntan, dilo: "Soy el asistente con IA de {spec.business_name}".
- Solo ayudas con este negocio: menú, precios, horario, entrega, pagos y pedidos. No eres un asistente de propósito general: si te piden cualquier otra cosa (tareas, noticias, recetas, consejos, chistes, programación, otros negocios), responde con amabilidad que solo ayudas con los pedidos de {spec.business_name}.

MENÚ (lo único que vendemos; precios en pesos colombianos)
{menu_lines(spec)}

HORARIO: {hours}
ENTREGA: {fulfillment_text(spec)}
PAGO: {payment_text(spec)}. El link de pago se abre dentro de WhatsApp; nunca pidas ni aceptes datos de tarjeta, claves ni documentos por el chat.

REGLAS
- Nunca inventes productos, precios, promociones, descuentos, combos, ingredientes, tiempos de entrega ni horarios que no estén arriba. Si no está arriba, no lo sabes.
- Si preguntan por algo que no está en el menú (por ejemplo cerveza u otra bebida), di que no lo vendemos y ofrece lo que sí hay. Nunca vendemos: {blocked}.
- Para pedir: cuando el cliente diga qué quiere y cuántas, usa crear_pedido con los product_id del menú. Si no es claro, pregunta o dile que toque el botón "{FLOW_CTA}". El link de pago se envía solo como botón aparte: no escribas URLs.
- Si no sabes algo, hay una queja, un pedido especial o el cliente quiere hablar con una persona, usa pasar_a_humano y dile que {owner} le responde pronto.
- Responde corto: máximo 2 o 3 frases, tono {spec.tone}. Español de Colombia. Sin markdown ni listas largas; para resaltar usa *asteriscos* como en WhatsApp.

DATOS DEL NEGOCIO (JSON, fuente de verdad; product_id para crear_pedido):
{spec_json(spec)}
"""


def spec_json(spec: BusinessSpec) -> str:
    """Compact, still-valid BusinessSpec JSON (no audio evidence) embedded in the system prompt."""
    return spec.model_dump_json(
        exclude={
            "evidence": True,
            "missing": True,
            "clarifying_question": True,
            "about": True,
            "description": True,
            "products": {"__all__": {"evidence", "photo"}},
            "hours": {"evidence"},
        }
    )


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOLS: list[dict] = [
    _tool("consultar_menu", "Devuelve el menú con precios, horario, forma de entrega y medios de pago del negocio.", {}, []),
    _tool(
        "crear_pedido",
        "Crea un pedido del cliente con productos del menú. Úsalo solo cuando el cliente ya dijo qué quiere y cuántas.",
        {
            "items": {
                "type": "array",
                "description": "Productos pedidos",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "description": "id del producto en el menú"},
                        "qty": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["product_id", "qty"],
                },
            },
            "pickup_time": {"type": "string", "description": "Hora de recogida HH:MM, si la dijo"},
            "customer_name": {"type": "string", "description": "Nombre del cliente, si lo dijo"},
        },
        ["items"],
    ),
    _tool("link_de_pago", "Envía al cliente el botón con el link de pago de un pedido suyo.", {"order_id": {"type": "string", "description": "id del pedido, ej. #3"}}, ["order_id"]),
    _tool("estado_pedido", "Consulta el estado de un pedido del cliente (el último si no se da id).", {"order_id": {"type": "string"}}, []),
    _tool("pasar_a_humano", "Avisa a la dueña para que atienda personalmente al cliente.", {"reason": {"type": "string", "description": "qué necesita el cliente"}}, ["reason"]),
]

# ------------------------------------------------------------------ guards

# Things a customer may ask for that this kind of business never sells (policy + no invention).
_OFF_MENU = {
    "alcohol": r"\b(cerveza|cervezas|pola|polas|aguardiente|guaro|ron|whisky|vino|licor|licores|trago|tragos|vodka|tequila|michelada)\b",
    "tobacco": r"\b(cigarrillo|cigarrillos|tabaco|vape|vapeador|pucho)\b",
    "gambling": r"\b(apuesta|apuestas|rifa|rifas|loteria)\b",
    "health": r"\b(medicamento|medicamentos|pastilla|pastillas|suplemento|suplementos)\b",
    "weapons": r"\b(armas|pistola|pistolas|municion)\b",
}


def _policy_guard(text: str, spec: BusinessSpec) -> str | None:
    """Deterministic refusal for policy-restricted items the menu does not have."""
    t = _norm(text)
    menu = _norm(" ".join(p.name + " " + p.description for p in spec.products))
    for key, rx in _OFF_MENU.items():
        m = re.search(rx, t)
        if m and m.group(0) not in menu:
            return f"No, aquí no vendemos {BLOCKED_CATEGORIES.get(key, m.group(0))}. En {spec.business_name} tenemos " + _menu_short(spec) + ". ¿Te provoca algo?"
    return None


def _menu_short(spec: BusinessSpec) -> str:
    parts = [f"{p.name.lower() if i else p.name} a {payments.money(p.price_cop)}" for i, p in enumerate(spec.products[:4])]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " y " + parts[-1]


_AMOUNT = re.compile(r"\$\s?\d{1,3}(?:[.,]\d{3})+|\$\s?\d+|\b\d{1,3}(?:\.\d{3})+\b|\b\d+(?:[.,]\d+)?\s?mil\b", re.I)


def amounts(text: str) -> set[int]:
    """Peso amounts mentioned in a reply: "$5.000", "5.000", "$5000", "5 mil" -> {5000}."""
    out: set[int] = set()
    for raw in _AMOUNT.findall(text or ""):
        s = raw.lower().replace("$", "").strip()
        if "mil" in s:
            num = s.replace("mil", "").strip().replace(",", ".")
            try:
                out.add(int(round(float(num) * 1000)))
            except ValueError:
                pass
        else:
            out.add(int(re.sub(r"[^\d]", "", s)))
    return out


def _allowed_amounts(spec: BusinessSpec, extra: set[int]) -> set[int]:
    return {p.price_cop * q for p in spec.products for q in range(1, 21)} | extra


def _price_guard(text: str, spec: BusinessSpec, extra: set[int]) -> str:
    """If the model mentions a peso amount that is not a menu price, a multiple of one or an order
    total from this conversation, replace the reply with the real menu (never invent prices)."""
    bad = amounts(text) - _allowed_amounts(spec, extra)
    if not bad:
        return text
    return f"Te cuento lo que tenemos en {spec.business_name}: {_menu_short(spec)}. ¿Qué te provoca?"


# ------------------------------------------------------------------ tools


def _match_product(spec: BusinessSpec, ref: Any):
    if not ref:
        return None
    ref = str(ref)
    p = spec.product(ref) or spec.product(slugify(ref))
    if p:
        return p
    n = _norm(ref)
    for p in spec.products:
        if n == _norm(p.name) or n in _norm(p.name) or _norm(p.name) in n:
            return p
    # loose: every word of the reference appears in the product name
    words = [w for w in re.findall(r"[a-z]+", n) if len(w) > 2 and w not in ("arepa", "arepas", "de", "con")]
    for p in spec.products:
        if words and all(w in _norm(p.name) for w in words):
            return p
    return None


class _Turn:
    """Tool executor for one customer turn; collects side effects for handle()."""

    def __init__(self, cradle: str, wa_id: str, biz: dict, test: bool) -> None:
        self.cradle, self.wa_id, self.biz, self.test = cradle, wa_id, biz, test
        self.spec: BusinessSpec = biz["spec"]
        self.actions: list[dict] = []
        self.order: dict | None = None
        self.totals: set[int] = {o["total_cop"] for o in store.list_orders(cradle, wa_id)[:10]}

    def run(self, name: str, args: dict) -> dict:
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return {"error": f"herramienta desconocida: {name}"}
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"error": f"argumentos inválidos: {exc}"}

    def t_consultar_menu(self, **_: Any) -> dict:
        s = self.spec
        return {
            "negocio": s.business_name,
            "productos": [{"product_id": p.id, "nombre": p.name, "precio_cop": p.price_cop, "descripcion": p.description} for p in s.products],
            "horario": hours_text(s) or "sin definir",
            "entrega": fulfillment_text(s),
            "pago": payment_text(s),
        }

    def t_crear_pedido(self, items: Any = None, pickup_time: str = "", customer_name: str = "", **_: Any) -> dict:
        if self.order is not None:  # one order per turn, even if the model calls the tool twice
            return {"ok": True, "order_id": self.order["id"], "total_cop": self.order["total_cop"], "nota": "pedido ya creado"}
        if isinstance(items, dict):
            items = [items]
        lines, unknown = [], []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            ref = it.get("product_id") or it.get("id") or it.get("product") or it.get("producto") or it.get("name") or it.get("nombre")
            p = _match_product(self.spec, ref)
            try:
                qty = int(it.get("qty") or it.get("quantity") or it.get("cantidad") or 1)
            except (TypeError, ValueError):
                qty = 1
            if p is None:
                unknown.append(str(ref))
                continue
            qty = max(1, min(qty, flowgen.MAX_QTY))
            prev = next((x for x in lines if x["product_id"] == p.id), None)
            if prev:
                prev["qty"] = min(prev["qty"] + qty, flowgen.MAX_QTY)
            else:
                lines.append({"product_id": p.id, "name": p.name, "qty": qty, "unit_cop": p.price_cop})
        if unknown or not lines:
            return {"ok": False, "error": "producto no está en el menú: " + ", ".join(unknown) if unknown else "pedido sin productos", "menu": [p.id for p in self.spec.products]}
        pt = (pickup_time or "").strip()
        order = {
            "items": lines,
            "total_cop": sum(x["qty"] * x["unit_cop"] for x in lines),
            "pickup_time": pt if re.fullmatch(r"\d{1,2}:\d{2}", pt) else "",
            "fulfillment": "pickup" if self.spec.fulfillment != Fulfillment.delivery else "delivery",
            "customer_name": (customer_name or "")[:80],
            "note": "",
            "source": "chat",
        }
        self.order = store.create_order(self.cradle, self.wa_id, order, test=self.test)
        self.totals.add(self.order["total_cop"])
        self.actions.append({"type": "order", "order": self.order})
        will_link = payments.accepts_link(self.spec)
        return {
            "ok": True,
            "order_id": self.order["id"],
            "total_cop": self.order["total_cop"],
            "items": lines,
            "pago": "se envía un botón con el link de pago" if will_link else "paga " + payment_text(self.spec),
        }

    def _own_order(self, order_id: str | None) -> dict | None:
        if order_id:
            o = store.get_order(order_id)
            return o if o and o["cradle_phone_id"] == self.cradle and o["customer_wa_id"] == self.wa_id else None
        orders = store.list_orders(self.cradle, self.wa_id)
        return orders[0] if orders else None

    def t_link_de_pago(self, order_id: str = "", **_: Any) -> dict:
        o = self._own_order(order_id)
        if o is None:
            return {"ok": False, "error": "no encuentro ese pedido de este cliente"}
        if not payments.accepts_link(self.spec):
            return {"ok": False, "error": "este negocio no recibe link de pago; paga " + payment_text(self.spec)}
        if o["status"] == "paid":
            return {"ok": False, "error": f"el pedido {o['id']} ya está pagado"}
        self.actions.append({"type": "pay_link", "order": o})
        self.totals.add(o["total_cop"])
        return {"ok": True, "order_id": o["id"], "total_cop": o["total_cop"], "nota": "botón de pago enviado aparte"}

    def t_estado_pedido(self, order_id: str = "", **_: Any) -> dict:
        o = self._own_order(order_id)
        if o is None:
            return {"ok": False, "error": "este cliente no tiene pedidos"}
        self.totals.add(o["total_cop"])
        estado = {"pending": "pendiente de pago", "paid": "pagado", "ready": "listo para recoger", "delivered": "entregado", "cancelled": "cancelado"}
        return {"ok": True, "order_id": o["id"], "estado": estado.get(o["status"], o["status"]), "total_cop": o["total_cop"], "hora": o.get("pickup_time", "")}

    def t_pasar_a_humano(self, reason: str = "", **_: Any) -> dict:
        self.actions.append({"type": "handoff", "reason": reason or "el cliente pidió hablar con una persona"})
        return {"ok": True, "nota": f"{self.spec.owner_name or 'la dueña'} ya fue avisada y responde pronto"}


# ------------------------------------------------------------------ core loop


def _messages(biz: dict, cradle: str, wa_id: str, text: str) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": biz.get("system_prompt") or system_prompt(biz["spec"])}]
    for h in store.history(cradle, wa_id, limit=12):
        if h["role"] in ("user", "assistant") and h["content"]:
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": text})
    return msgs


def _reply(cradle: str, wa_id: str, text: str, *, test: bool = False) -> dict:
    biz = store.get_business(cradle)
    if biz is None:
        raise KeyError(f"no hay negocio en {cradle}")
    spec: BusinessSpec = biz["spec"]
    turn = _Turn(cradle, wa_id, biz, test)

    guarded = _policy_guard(text, spec)
    if guarded:
        return {"text": guarded, "tool_calls": [], "actions": [], "order": None, "guard": "policy"}

    msgs = _messages(biz, cradle, wa_id, text)
    calls: list[dict] = []
    final = ""
    for _round in range(MAX_ROUNDS):
        out = muse.chat(msgs, TOOLS)
        tcs = out.get("tool_calls") or []
        if not tcs:
            final = (out.get("content") or "").strip()
            break
        msgs.append(
            {
                "role": "assistant",
                "content": out.get("content") or "",
                "tool_calls": [
                    {"id": tc.get("id") or f"call_{len(calls) + i}", "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
                    for i, tc in enumerate(tcs)
                ],
            }
        )
        for i, tc in enumerate(tcs):
            args = tc.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            result = turn.run(tc.get("name", ""), args)
            calls.append({"name": tc.get("name", ""), "arguments": args, "result": result})
            msgs.append({"role": "tool", "tool_call_id": tc.get("id") or f"call_{len(calls) - 1 + i}", "content": json.dumps(result, ensure_ascii=False, default=str)})
    if not final:
        final = _fallback_text(turn)
    final = _price_guard(final, spec, turn.totals)
    return {"text": final, "tool_calls": calls, "actions": turn.actions, "order": turn.order}


def _fallback_text(turn: _Turn) -> str:
    """Text when the model kept calling tools for MAX_ROUNDS or returned nothing."""
    if turn.order:
        return f"¡Listo! Tu pedido {turn.order['id']} quedó por {payments.money(turn.order['total_cop'])}."
    if any(a["type"] == "handoff" for a in turn.actions):
        return f"Ya le avisé a {turn.spec.owner_name or 'la dueña'}, te responde pronto."
    return f"Te cuento lo que tenemos en {turn.spec.business_name}: {_menu_short(turn.spec)}. ¿Qué te provoca?"


def reply_text(cradle_phone_id: str, wa_id: str, text: str, *, test: bool = False) -> dict:
    """Agent answer without sending anything: {"text", "tool_calls", "actions", "order"}.

    Used by the inspector. Tool side effects that live in the store (crear_pedido) do happen; with
    test=True those orders are marked as test orders.
    """
    return _reply(cradle_phone_id, wa_id, text, test=test)


# ------------------------------------------------------------------ WhatsApp entry point


_ORDER_INTENT = re.compile(r"\b(quiero|quisiera|pedir|pedido|encargar|encargo|me (?:da|das|regala|regalas|vende|vendes)|para llevar)\b")


def _intro(spec: BusinessSpec) -> str:
    return f"Hola, soy el asistente con IA de {spec.business_name}."


def _send_flow(biz: dict, cradle: str, to: str, body: str = "") -> str | None:
    if not biz.get("flow_id"):
        return None
    return whatsapp.send_flow(cradle, to, body or "Arma tu pedido aquí y te mando el link de pago.", biz["flow_id"], FLOW_CTA, FLOW_SCREEN, None, flow_token=f"order:{cradle}:{to}")


def _send_order(biz: dict, cradle: str, to: str, order: dict) -> list[str]:
    """Customer confirmation (+ payment link button) and owner notification for a new order."""
    spec: BusinessSpec = biz["spec"]
    sent: list[str] = []
    summary = payments.order_summary(order)
    url = ""
    if payments.accepts_link(spec):
        try:
            url = payments.link(order, spec)
        except Exception:  # noqa: BLE001 - the customer must still get an answer
            log.exception("payment link failed for order %s", order.get("id"))
        if url:
            sent.append(whatsapp.send_cta_url(cradle, to, f"{summary}\n\nPaga seguro con Mercado Pago, se abre aquí mismo en WhatsApp.", f"Pagar {payments.money(order['total_cop'])}", url))
        else:
            sent.append(whatsapp.send_text(cradle, to, f"{summary}\n\nEn un momento te mando el link de pago."))
    else:
        sent.append(whatsapp.send_text(cradle, to, f"{summary}\n\nPagas {payment_text(spec)} al recoger."))
    _notify_owner(cradle, f"Nuevo pedido en {spec.business_name}\n{summary}" + ("" if not payments.accepts_link(spec) or url else "\n(No salió el link de pago: cóbrale por otro medio.)"))
    return sent


def _notify_owner(cradle: str, text: str) -> None:
    try:
        payments.notify_owner(cradle, text)
    except Exception:  # noqa: BLE001 - a failed notification must not cut the customer's flow
        log.exception("owner notification failed on %s", cradle)


def _handle_flow(biz: dict, cradle: str, inbound: whatsapp.Inbound) -> list[str]:
    spec: BusinessSpec = biz["spec"]
    try:
        parsed = flowgen.parse_order(inbound.flow_response or {}, spec)
    except ValueError:
        return [whatsapp.send_text(cradle, inbound.wa_id, "No alcancé a ver productos en tu pedido. ¿Lo intentas de nuevo eligiendo la cantidad?")]
    if not parsed.get("customer_name"):
        parsed["customer_name"] = inbound.name
    parsed["source"] = "flow"
    order = store.create_order(cradle, inbound.wa_id, parsed)
    store.append(cradle, inbound.wa_id, "user", "[Pedido por formulario] " + payments.order_lines(order).replace("\n", "; "))
    store.append(cradle, inbound.wa_id, "assistant", f"Pedido {order['id']} creado por {payments.money(order['total_cop'])}.")
    return _send_order(biz, cradle, inbound.wa_id, order)


def handle(cradle_phone_id: str, inbound: whatsapp.Inbound) -> list[str]:
    """Process one inbound message on a cradle number. Returns the ids of the messages sent."""
    biz = store.get_business(cradle_phone_id)
    if biz is None or not store.mark_seen(inbound.message_id):
        return []
    spec: BusinessSpec = biz["spec"]
    to = inbound.wa_id
    if not biz["live"] and to not in {settings.test_customer_wa_id, biz.get("owner_wa_id")} - {""}:
        return [whatsapp.send_text(cradle_phone_id, to, f"¡Hola! {spec.business_name} aún no abre por este WhatsApp. Muy pronto te atendemos por aquí.")]
    if inbound.message_id:
        whatsapp.mark_read(cradle_phone_id, inbound.message_id)

    if inbound.flow_response is not None:
        return _handle_flow(biz, cradle_phone_id, inbound)

    text = inbound.text or ""
    if inbound.button_id in ORDER_BUTTON_IDS or _norm(text) in ("hacer pedido", "pedir"):
        mid = _send_flow(biz, cradle_phone_id, to)
        if mid:
            return [mid]
    if inbound.type in ("audio", "voice") and inbound.media_id:
        try:
            data, mime = whatsapp.download_media(inbound.media_id)
            text = muse.transcribe(data, mime or inbound.mime or "audio/ogg").text
        except Exception:  # noqa: BLE001
            return [whatsapp.send_text(cradle_phone_id, to, "No pude escuchar tu audio. ¿Me lo escribes, porfa?")]
    if not text.strip():
        return [whatsapp.send_text(cradle_phone_id, to, f"{_intro(spec)} Por aquí te ayudo con el menú y tus pedidos. Escríbeme qué te provoca.")]

    first = not store.history(cradle_phone_id, to, limit=1)
    try:
        res = reply_text(cradle_phone_id, to, text)
    except Exception:  # noqa: BLE001 - never leave the customer without an answer
        res = {"text": f"Uy, se me enredó algo. Ya le aviso a {spec.owner_name or 'la dueña'} para que te responda.", "tool_calls": [], "actions": [{"type": "handoff", "reason": "error del asistente"}], "order": None}
    reply = res["text"]
    if first and "IA" not in reply:
        reply = f"{_intro(spec)} {reply}"
    store.append(cradle_phone_id, to, "user", text)
    store.append(cradle_phone_id, to, "assistant", reply)

    sent = [whatsapp.send_text(cradle_phone_id, to, reply)]
    for action in res.get("actions", []):
        if action["type"] == "order":
            sent += _send_order(biz, cradle_phone_id, to, action["order"])
        elif action["type"] == "pay_link":
            o = action["order"]
            try:
                url = o.get("pay_url") or payments.link(o, spec)
                sent.append(whatsapp.send_cta_url(cradle_phone_id, to, f"Pedido {o['id']}: {payments.money(o['total_cop'])}", f"Pagar {payments.money(o['total_cop'])}", url))
            except Exception:  # noqa: BLE001
                log.exception("payment link failed for order %s", o.get("id"))
        elif action["type"] == "handoff":
            who = inbound.name or to
            _notify_owner(cradle_phone_id, f"{who} (+{to}) necesita que lo atiendas en {spec.business_name}: {action['reason']}\nÚltimo mensaje: {text[:300]}")
    if not res.get("order") and _ORDER_INTENT.search(_norm(text)):
        mid = _send_flow(biz, cradle_phone_id, to, "Aquí armas tu pedido:")
        if mid:
            sent.append(mid)
    return sent

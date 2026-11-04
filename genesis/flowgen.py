"""BusinessSpec -> WhatsApp Flow JSON (order form), a local linter, and the order parser.

The Flow has no data_exchange endpoint: ORDER navigates to CONFIRM with a payload, CONFIRM completes
with the same payload, and the business receives it as `nfm_reply.response_json`. Prices never travel
through the Flow: `parse_order` recomputes every amount from the spec.

Flow JSON reference: https://developers.facebook.com/docs/whatsapp/flows/reference/flowjson
Components: https://developers.facebook.com/docs/whatsapp/flows/reference/components
Version 7.3 is the one Meta's changelog marks as recommended for publishing (checked 2026-11-04).
"""

from __future__ import annotations

import re
from typing import Any

from .spec import BusinessSpec, Fulfillment, Product

FLOW_JSON_VERSION = "7.3"
MAX_PRODUCTS = 8
MAX_QTY = 10
SLOT_MINUTES = 30
DEFAULT_OPEN, DEFAULT_CLOSE = "08:00", "18:00"

# Character limits from the components reference.
LABEL_LIMITS = {
    "Dropdown": 20,
    "RadioButtonsGroup": 30,
    "CheckboxGroup": 30,
    "TextInput": 20,
    "TextArea": 20,
    "Footer": 35,
    "DatePicker": 40,
}
TEXT_LIMITS = {"TextHeading": 80, "TextSubheading": 80, "TextBody": 4096, "TextCaption": 4096}
OPTION_TITLE_LIMIT = 30
OPTION_LIMITS = {"Dropdown": 200, "RadioButtonsGroup": 20, "CheckboxGroup": 20}
INPUT_TYPES = {"Dropdown", "RadioButtonsGroup", "CheckboxGroup", "TextInput", "TextArea", "DatePicker", "OptIn", "ChipsSelector"}
MAX_COMPONENTS_PER_SCREEN = 50

_SCREEN_ID = re.compile(r"^[A-Z][A-Z_]*$")
_REF = re.compile(r"\$\{(form|data)\.([A-Za-z0-9_]+)\}")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------- helpers shared with other modules


def qty_field(product: Product) -> str:
    """Name of the quantity dropdown for a product (Flow names use underscores, not dashes)."""
    return "qty_" + re.sub(r"[^a-z0-9_]", "_", product.id.lower())


def flow_products(spec: BusinessSpec) -> list[Product]:
    """Products shown in the Flow (first MAX_PRODUCTS)."""
    return list(spec.products[:MAX_PRODUCTS])


def cop(n: int) -> str:
    return "$" + f"{int(n):,}".replace(",", ".")


def _minutes(hhmm: str) -> int | None:
    m = _HHMM.match((hhmm or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def friendly_time(hhmm: str) -> str:
    """'06:30' -> '6:30 a. m.' (Colombian style)."""
    mins = _minutes(hhmm)
    if mins is None:
        return hhmm
    h, m = divmod(mins, 60)
    suffix = "a. m." if h < 12 else "p. m."
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def pickup_slots(spec: BusinessSpec) -> list[str]:
    """Every 30 min from opening until 30 min before closing (wraps past midnight)."""
    start = _minutes(spec.hours.open) if spec.hours else None
    end = _minutes(spec.hours.close) if spec.hours else None
    if start is None or end is None or start == end:
        start, end = _minutes(DEFAULT_OPEN), _minutes(DEFAULT_CLOSE)
    assert start is not None and end is not None
    if end <= start:
        end += 24 * 60
    # Round the first slot up to the next half hour.
    first = -(-start // SLOT_MINUTES) * SLOT_MINUTES
    slots = [_hhmm(t) for t in range(first, end, SLOT_MINUTES)]
    return slots[:200] or [_hhmm(start)]


def _option_title(text: str) -> str:
    return text if len(text) <= OPTION_TITLE_LIMIT else text[: OPTION_TITLE_LIMIT - 1].rstrip() + "…"


def _label(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- Flow JSON


def order_flow(spec: BusinessSpec) -> dict:
    products = flow_products(spec)
    slots = pickup_slots(spec)
    delivery_only = spec.fulfillment == Fulfillment.delivery
    time_label = "Hora de entrega" if delivery_only else "Hora de recogida"

    menu_lines = "\n".join(f"• {p.name}: {cop(p.price_cop)}" for p in products)
    qty_options = [{"id": str(i), "title": str(i)} for i in range(MAX_QTY + 1)]

    order_children: list[dict[str, Any]] = [
        {"type": "TextHeading", "text": _label(spec.business_name, 80)},
        {"type": "TextBody", "text": f"Elige cuántas quieres de cada una.\n{menu_lines}"},
    ]
    for p in products:
        order_children.append(
            {
                "type": "Dropdown",
                "name": qty_field(p),
                "label": _label(p.name, LABEL_LIMITS["Dropdown"]),
                "required": False,
                "init-value": "0",
                "data-source": qty_options,
            }
        )
    order_children.append(
        {
            "type": "Dropdown",
            "name": "pickup_time",
            "label": time_label,
            "required": True,
            "data-source": [{"id": s, "title": friendly_time(s)} for s in slots],
        }
    )
    if spec.fulfillment == Fulfillment.both:
        zone = f" ({spec.delivery_zone})" if spec.delivery_zone else ""
        order_children.append(
            {
                "type": "RadioButtonsGroup",
                "name": "fulfillment",
                "label": "¿Cómo lo quieres?",
                "required": True,
                "data-source": [
                    {"id": "pickup", "title": "Recojo en el puesto"},
                    {"id": "delivery", "title": _option_title(f"Domicilio{zone}")},
                ],
            }
        )
    order_children += [
        {"type": "TextInput", "name": "customer_name", "label": "Tu nombre", "input-type": "text", "required": True},
        {"type": "TextArea", "name": "note", "label": "Nota (opcional)", "required": False},
    ]

    fields = [qty_field(p) for p in products] + ["pickup_time", "customer_name", "note"]
    if spec.fulfillment == Fulfillment.both:
        fields.append("fulfillment")

    order_children.append(
        {
            "type": "Footer",
            "label": "Continuar",
            "on-click-action": {
                "name": "navigate",
                "next": {"type": "screen", "name": "CONFIRM"},
                "payload": {f: "${form." + f + "}" for f in fields},
            },
        }
    )

    confirm_data = {f: {"type": "string", "__example__": _example(f)} for f in fields}
    confirm_children: list[dict[str, Any]] = [
        {"type": "TextHeading", "text": "Confirma tu pedido"},
        {"type": "TextSubheading", "text": time_label},
        {"type": "TextBody", "text": "${data.pickup_time}"},
        {
            "type": "TextBody",
            "text": "Al confirmar te mandamos por este chat el total y el link de pago. "
            "Pagas en el navegador de WhatsApp, nunca con datos de tarjeta en el chat.",
        },
        {
            "type": "Footer",
            "label": "Confirmar pedido",
            "on-click-action": {"name": "complete", "payload": {f: "${data." + f + "}" for f in fields}},
        },
    ]

    return {
        "version": FLOW_JSON_VERSION,
        "screens": [
            {
                "id": "ORDER",
                "title": _label(f"Pedido {spec.business_name}", 30),
                "data": {},
                "layout": {"type": "SingleColumnLayout", "children": order_children},
            },
            {
                "id": "CONFIRM",
                "title": "Confirmar",
                "terminal": True,
                "success": True,
                "data": confirm_data,
                "layout": {"type": "SingleColumnLayout", "children": confirm_children},
            },
        ],
    }


def _example(field_name: str) -> str:
    if field_name.startswith("qty_"):
        return "1"
    return {"pickup_time": "07:00", "customer_name": "Carlos", "note": "Sin cebolla", "fulfillment": "pickup"}.get(
        field_name, "x"
    )


def example_response(spec: BusinessSpec, quantities: dict[str, int] | None = None, **extra: str) -> dict:
    """A synthetic nfm_reply response_json (used by the inspector's test order and by tests).

    quantities maps product_id -> qty; default is 2 of the first product.
    """
    products = flow_products(spec)
    quantities = quantities if quantities is not None else {products[0].id: 2}
    resp: dict[str, Any] = {qty_field(p): str(quantities.get(p.id, 0)) for p in products}
    resp["pickup_time"] = pickup_slots(spec)[0]
    resp["customer_name"] = "Cliente de prueba"
    resp["note"] = ""
    if spec.fulfillment == Fulfillment.both:
        resp["fulfillment"] = "pickup"
    resp["flow_token"] = "test"
    resp.update(extra)
    return resp


# ---------------------------------------------------------------- lint


def _walk(children: list[Any]) -> list[dict]:
    """Flatten components, descending into Form / If / Switch containers."""
    out: list[dict] = []
    for c in children or []:
        if not isinstance(c, dict):
            continue
        out.append(c)
        if isinstance(c.get("children"), list):
            out += _walk(c["children"])
        for branch in ("then", "else"):
            if isinstance(c.get(branch), list):
                out += _walk(c[branch])
        if isinstance(c.get("cases"), dict):
            for v in c["cases"].values():
                if isinstance(v, list):
                    out += _walk(v)
    return out


def _refs(obj: Any) -> list[tuple[str, str]]:
    if isinstance(obj, str):
        return _REF.findall(obj)
    if isinstance(obj, dict):
        return [r for v in obj.values() for r in _refs(v)]
    if isinstance(obj, list):
        return [r for v in obj for r in _refs(v)]
    return []


def lint(flow_json: dict) -> list[str]:
    """Local checks of the basic Flow JSON rules. Returns human-readable problems ([] = ok)."""
    errs: list[str] = []
    if not isinstance(flow_json, dict):
        return ["flow json must be an object"]
    if not isinstance(flow_json.get("version"), str) or not flow_json.get("version"):
        errs.append("missing 'version' string")
    screens = flow_json.get("screens")
    if not isinstance(screens, list) or not screens:
        return errs + ["'screens' must be a non-empty list"]

    ids = [s.get("id") for s in screens if isinstance(s, dict)]
    seen: set[str] = set()
    for sid in ids:
        if not isinstance(sid, str) or not _SCREEN_ID.match(sid):
            errs.append(f"screen id {sid!r} must be uppercase letters/underscores")
        if sid == "SUCCESS":
            errs.append("screen id 'SUCCESS' is reserved")
        if sid in seen:
            errs.append(f"duplicate screen id {sid!r}")
        seen.add(sid)

    if not any(isinstance(s, dict) and s.get("terminal") for s in screens):
        errs.append("no terminal screen")

    for s in screens:
        if not isinstance(s, dict):
            errs.append("screen must be an object")
            continue
        sid = s.get("id")
        layout = s.get("layout") or {}
        if layout.get("type") != "SingleColumnLayout":
            errs.append(f"{sid}: layout.type must be SingleColumnLayout")
        comps = _walk(layout.get("children") or [])
        if not comps:
            errs.append(f"{sid}: empty layout")
        if len(comps) > MAX_COMPONENTS_PER_SCREEN:
            errs.append(f"{sid}: {len(comps)} components (max {MAX_COMPONENTS_PER_SCREEN})")

        footers = [c for c in comps if c.get("type") == "Footer"]
        if len(footers) > 1:
            errs.append(f"{sid}: more than one Footer")
        if s.get("terminal") and not footers:
            errs.append(f"{sid}: terminal screen needs a Footer")

        data_decl = s.get("data") or {}
        for key, decl in data_decl.items():
            if not isinstance(decl, dict) or "__example__" not in decl:
                errs.append(f"{sid}: data.{key} needs __example__")

        names: set[str] = set()
        for c in comps:
            ctype = c.get("type", "")
            if not ctype:
                errs.append(f"{sid}: component without type")
                continue
            if ctype in INPUT_TYPES:
                name = c.get("name")
                if not name:
                    errs.append(f"{sid}: {ctype} without name")
                elif name in names:
                    errs.append(f"{sid}: duplicate component name {name!r}")
                else:
                    names.add(name)
            label = c.get("label")
            limit = LABEL_LIMITS.get(ctype)
            if limit is not None:
                if not isinstance(label, str) or not label.strip():
                    errs.append(f"{sid}: {ctype} {c.get('name', '')} needs a label")
                elif "${" not in label and len(label) > limit:
                    errs.append(f"{sid}: {ctype} label {label!r} longer than {limit}")
            tlimit = TEXT_LIMITS.get(ctype)
            if tlimit is not None:
                text = c.get("text")
                if not isinstance(text, str) or not text.strip():
                    errs.append(f"{sid}: {ctype} with empty text")
                elif len(text) > tlimit:
                    errs.append(f"{sid}: {ctype} text longer than {tlimit}")
            if ctype in OPTION_LIMITS:
                ds = c.get("data-source")
                if isinstance(ds, list):
                    if not ds:
                        errs.append(f"{sid}: {c.get('name')} data-source is empty")
                    if len(ds) > OPTION_LIMITS[ctype]:
                        errs.append(f"{sid}: {c.get('name')} has {len(ds)} options (max {OPTION_LIMITS[ctype]})")
                    opt_ids = [o.get("id") for o in ds if isinstance(o, dict)]
                    if len(opt_ids) != len(set(opt_ids)) or any(not isinstance(i, str) for i in opt_ids):
                        errs.append(f"{sid}: {c.get('name')} option ids must be unique strings")
                    for o in ds:
                        title = o.get("title") if isinstance(o, dict) else None
                        if not isinstance(title, str) or not title:
                            errs.append(f"{sid}: {c.get('name')} option without title")
                        elif len(title) > OPTION_TITLE_LIMIT:
                            errs.append(f"{sid}: option title {title!r} longer than {OPTION_TITLE_LIMIT}")
                    init = c.get("init-value")
                    if isinstance(init, str) and init not in opt_ids:
                        errs.append(f"{sid}: {c.get('name')} init-value {init!r} is not an option")
                elif not (isinstance(ds, str) and ds.startswith("${")):
                    errs.append(f"{sid}: {c.get('name')} needs a data-source")

        # References: ${form.x} must name an input on this screen, ${data.x} must be declared.
        for c in comps:
            for kind, key in _refs(c):
                if kind == "form" and key not in names:
                    errs.append(f"{sid}: ${{form.{key}}} does not match any input")
                if kind == "data" and key not in data_decl:
                    errs.append(f"{sid}: ${{data.{key}}} is not declared in screen data")

        for f in footers:
            action = f.get("on-click-action") or {}
            aname = action.get("name")
            if aname == "navigate":
                nxt = (action.get("next") or {}).get("name")
                if nxt not in seen:
                    errs.append(f"{sid}: navigate to unknown screen {nxt!r}")
                else:
                    target = next(x for x in screens if isinstance(x, dict) and x.get("id") == nxt)
                    missing = set(action.get("payload") or {}) - set(target.get("data") or {})
                    if missing:
                        errs.append(f"{sid}: payload keys {sorted(missing)} not declared in {nxt}.data")
            elif aname == "complete":
                if not s.get("terminal"):
                    errs.append(f"{sid}: complete action on a non-terminal screen")
            elif aname not in ("data_exchange", "open_url", "update_data"):
                errs.append(f"{sid}: Footer needs on-click-action navigate|complete")

    routing = flow_json.get("routing_model")
    if isinstance(routing, dict):
        for src, dsts in routing.items():
            for d in [src, *(dsts or [])]:
                if d not in seen:
                    errs.append(f"routing_model references unknown screen {d!r}")
    return errs


# ---------------------------------------------------------------- parse


def parse_order(response_json: dict, spec: BusinessSpec) -> dict:
    """nfm_reply.response_json -> order. Prices always come from the spec, never from the client.

    Raises ValueError when the order has no items.
    """
    if not isinstance(response_json, dict):
        raise ValueError("response_json must be an object")
    items = []
    for p in flow_products(spec):
        raw = response_json.get(qty_field(p), 0)
        try:
            qty = int(float(str(raw).strip() or 0))
        except ValueError:
            qty = 0
        qty = max(0, min(qty, MAX_QTY))
        if qty > 0:
            items.append({"product_id": p.id, "name": p.name, "qty": qty, "unit_cop": p.price_cop})
    if not items:
        raise ValueError("order has no items with quantity > 0")

    pickup = str(response_json.get("pickup_time") or "").strip()
    if _minutes(pickup) is None:
        pickup = ""
    elif pickup not in pickup_slots(spec):
        pickup = _hhmm(_minutes(pickup) or 0)  # normalize "7:00" -> "07:00", keep the customer's time

    fulfillment = spec.fulfillment.value
    if spec.fulfillment == Fulfillment.both:
        chosen = str(response_json.get("fulfillment") or "").strip()
        fulfillment = chosen if chosen in ("pickup", "delivery") else "pickup"

    return {
        "items": items,
        "total_cop": sum(i["qty"] * i["unit_cop"] for i in items),
        "pickup_time": pickup,
        "fulfillment": fulfillment,
        "customer_name": str(response_json.get("customer_name") or "").strip()[:80],
        "note": str(response_json.get("note") or "").strip()[:600],
    }

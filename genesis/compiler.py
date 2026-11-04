"""Voice note + photos -> BusinessSpec, at most ONE clarifying question, and the read-back text.

Muse Spark does the understanding (structured output with the BusinessSpec JSON Schema); this module
adds deterministic guard rails around it: Colombian price parsing ("cinco mil", "a 5"), hour
normalization ("6 a 11 de la mañana"), evidence seconds snapped to transcript segments, a keyword
backstop for blocked categories, and a single retry with the validation error.
"""

from __future__ import annotations

import importlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from . import spec as spec_mod
from .config import settings
from .spec import BLOCKED_CATEGORIES, BusinessSpec, Fulfillment, PaymentMethod, slugify

if TYPE_CHECKING:  # muse.py is written by another module owner; import lazily at call time
    from . import muse


def _muse():
    return importlib.import_module("genesis.muse")


@dataclass
class Draft:
    spec: BusinessSpec
    transcript: "muse.Transcript"
    question: str  # "" if nothing essential missing; else ONE question in Colombian Spanish
    blocked: str | None  # BLOCKED_CATEGORIES key if refused


class CompileError(ValueError):
    """The model output could not be turned into a valid BusinessSpec after one retry."""


# ---------------------------------------------------------------- prompt

SYSTEM = (
    "Eres el compilador de Génesis. Conviertes la nota de voz de un vendedor informal colombiano "
    "en una especificación de negocio (BusinessSpec) en JSON que sigue el esquema exacto. "
    "Nunca inventes productos ni precios que el vendedor no dijo. Responde solo con el JSON."
)

DOMAIN_RULES = """Contexto: vendedores informales de Cali, Colombia (arepas, empanadas, jugos, postres, ropa...).
Reglas:
- Precios en pesos colombianos (COP) como entero: "cinco mil" = 5000, "a 5" o "a cinco" = 5000,
  "dos mil quinientos" o "2.500" = 2500, "luquitas"/"lucas" = miles. Si un producto no tiene precio, price_cop = 0
  y agrega "price:<id>" a "missing".
- Horarios en 24 h "HH:MM": "6 a 11 de la mañana" = open "06:00", close "11:00"; "de 2 a 8 de la noche" = "14:00"-"20:00".
  days con códigos mon,tue,wed,thu,fri,sat,sun ("de lunes a sábado" = mon..sat). Sin horario: hours = null y
  agrega "hours" a "missing".
- fulfillment: "pickup" si recogen en el puesto, "delivery" si solo domicilio, "both" si ambas. Si no lo dijo,
  usa "pickup" y agrega "fulfillment" a "missing".
- payment_methods: cash (efectivo), link (link de pago), transfer (Nequi, Daviplata, Bancolombia, transferencia).
- business_name: si no dijo nombre, invéntalo como "<Producto> <Nombre de la dueña/dueño>" (ej. "Arepas Rosa").
- product id: slug en minúsculas con guiones. photo: índice (desde 0) de la foto que muestra el producto, o null.
- evidence: para cada producto, para hours y para los campos de arriba (business_name, owner_name, category,
  neighborhood, city, fulfillment, payment_methods) llena source_s con el segundo de inicio del segmento
  del audio donde se dijo (usa los tiempos [inicio-fin] del transcript) y quote con las palabras exactas.
- blocked_category: si vende algo de estas categorías pon la clave, si no null: {blocked}.
- about: máximo 139 caracteres, para el perfil de WhatsApp. description: máximo 512 caracteres.
- tone: cómo debe hablar el asistente (por defecto "cercano, colombiano, breve").
- missing: campos que no se pudieron inferir. clarifying_question: UNA sola pregunta corta, en español
  colombiano cercano, solo si falta algo esencial (nombre, al menos un producto con precio, modo de entrega);
  si no falta nada esencial, "".
"""


def _fmt_segments(transcript: Any) -> str:
    segs = getattr(transcript, "segments", None) or []
    if not segs:
        return transcript.text or "(sin audio)"
    return "\n".join(f"[{float(s.get('start', 0)):.1f}-{float(s.get('end', 0)):.1f}] {s.get('text', '').strip()}" for s in segs)


def _build_prompt(transcript: Any, n_photos: int, answer: str = "", previous: Draft | None = None) -> str:
    blocked = json.dumps(BLOCKED_CATEGORIES, ensure_ascii=False)
    parts = [DOMAIN_RULES.format(blocked=blocked), "Transcripción de la nota de voz (con segundos):", _fmt_segments(transcript)]
    parts.append(f"Fotos adjuntas: {n_photos} (índices 0..{n_photos - 1})." if n_photos else "No hay fotos.")
    if previous is not None:
        parts += [
            "Especificación anterior (consérvala y solo corrige o completa lo que responde el vendedor):",
            previous.spec.model_dump_json(),
            f'Le preguntamos: "{previous.question or previous.spec.clarifying_question}"',
            f'El vendedor respondió: "{answer}"',
            "Integra la respuesta. Ya no se puede hacer otra pregunta: clarifying_question debe ser \"\".",
        ]
    elif answer:
        parts.append(f'Información adicional del vendedor: "{answer}"')
    return "\n\n".join(parts)


# ---------------------------------------------------------------- deterministic normalization

_UNITS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
    "veintiuno": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30, "cuarenta": 40, "cincuenta": 50,
    "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100, "doscientos": 200,
    "trescientos": 300, "cuatrocientos": 400, "quinientos": 500, "seiscientos": 600, "setecientos": 700,
    "ochocientos": 800, "novecientos": 900, "media": 0.5, "medio": 0.5,
}


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def _words_to_number(text: str) -> float | None:
    total, current, seen = 0.0, 0.0, False
    for w in re.findall(r"[a-z]+|\d+(?:[.,]\d+)?", _norm(text)):
        if w in ("y", "pesos", "peso", "a", "por", "de", "cada", "una", "la", "el", "cop", "luca", "lucas", "luquitas"):
            if w in ("luca", "lucas", "luquitas") and seen:
                total += current * 1000
                current = 0
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)?", w):
            current += _digits(w)
            seen = True
        elif w in _UNITS:
            if w in ("media", "medio") and seen:  # "cinco mil y medio" -> +500, "a 5 y medio" -> 5.5
                current += 500 if total >= 1000 else 0.5
            else:
                current += _UNITS[w]
            seen = True
        elif w in ("mil", "k"):
            total += (current or 1) * 1000
            current = 0
            seen = True
        elif w in ("millon", "millones"):
            total += (current or 1) * 1_000_000
            current = 0
            seen = True
    return total + current if seen else None


def _digits(s: str) -> float:
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s) or re.fullmatch(r"\d{1,3}(?:,\d{3})+", s):
        return float(re.sub(r"[.,]", "", s))  # 5.000 / 5,000 thousands separators
    return float(s.replace(",", "."))


def parse_cop(value: Any) -> int:
    """Colombian street price -> integer COP. 'cinco mil' -> 5000, 'a 5' -> 5000, '$2.500' -> 2500."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        s = _norm(str(value)).replace("$", " ")
        m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*k\s*", s)
        n = _digits(m.group(1)) * 1000 if m else (_words_to_number(s) or 0.0)
    if 0 < n < 100:  # "a 5", "a 2.5": street shorthand for thousands
        n *= 1000
    return max(0, int(round(n)))


_HOUR_WORDS = {k: v for k, v in _UNITS.items() if isinstance(v, int) and 0 < v <= 12}


def parse_hhmm(value: Any, default_pm: bool = False) -> str | None:
    """'6' / '6am' / '6:30 pm' / '18:00' / 'seis de la mañana' / '11 de la noche' -> 'HH:MM'."""
    if value is None:
        return None
    s = _norm(str(value))
    m = re.search(r"(\d{1,2})(?:[:.h](\d{2}))?", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
    else:
        word = next((w for w in re.findall(r"[a-z]+", s) if w in _HOUR_WORDS), None)
        if word is None:
            return None
        h, mi = _HOUR_WORDS[word], 30 if "media" in s else 0
    pm = bool(re.search(r"\bp\.? ?m\b|tarde|noche", s))
    am = bool(re.search(r"\ba\.? ?m\b|manana|madrugada", s))
    if pm and h < 12:
        h += 12
    elif am and h == 12:
        h = 0
    elif not am and not pm and default_pm and h < 12:
        h += 12
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


_DAYS = {
    "lunes": "mon", "lun": "mon", "mon": "mon", "martes": "tue", "mar": "tue", "tue": "tue",
    "miercoles": "wed", "mie": "wed", "mier": "wed", "wed": "wed", "jueves": "thu", "jue": "thu", "thu": "thu",
    "viernes": "fri", "vie": "fri", "fri": "fri", "sabado": "sat", "sab": "sat", "sat": "sat",
    "domingo": "sun", "dom": "sun", "sun": "sun",
}
_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_ES = {"mon": "lun", "tue": "mar", "wed": "mié", "thu": "jue", "fri": "vie", "sat": "sáb", "sun": "dom"}


def _norm_days(days: Any) -> list[str]:
    if isinstance(days, str):
        days = re.split(r"[,\s/]+", days)
    out = []
    for d in days or []:
        code = _DAYS.get(_norm(d).strip(". "))
        if code and code not in out:
            out.append(code)
    return sorted(out, key=_WEEK.index)


_BLOCK_KEYWORDS = {
    "alcohol": ["cerveza", "cervezas", "aguardiente", "guaro", "licor", "licores", "ron", "whisky", "vino", "vinos", "tequila", "vodka", "michelada"],
    "tobacco": ["cigarrillo", "cigarrillos", "tabaco", "vape", "vapes", "vapeador", "vapeadores", "hookah", "narguila"],
    "gambling": ["rifa", "rifas", "apuesta", "apuestas", "chance", "loteria", "bingo"],
    "finance": ["prestamo", "prestamos", "gota a gota", "creditos", "inversiones"],
    "health": ["medicamento", "medicamentos", "pastillas", "suplemento", "suplementos", "farmacia", "adelgazante", "remedios"],
    "weapons": ["armas", "pistola", "pistolas", "municion", "navajas"],
    "adult": ["contenido para adultos", "erotico", "sex shop", "juguetes sexuales"],
}


def detect_blocked(spec_data: dict) -> str | None:
    """Keyword backstop over what is being sold (category + products), not the whole chat."""
    hay = " " + _norm(
        " ".join(
            [str(spec_data.get("category", "")), str(spec_data.get("business_name", ""))]
            + [f"{p.get('name', '')} {p.get('description', '')}" for p in spec_data.get("products") or [] if isinstance(p, dict)]
        )
    ) + " "
    for key, words in _BLOCK_KEYWORDS.items():
        for w in words:
            if re.search(r"\b" + re.escape(w.strip()) + r"\b", hay):
                return key
    return None


def _locate(segments: list[dict], *needles: str) -> float | None:
    """Start second of the segment that best matches the needles (quote or name)."""
    if not segments:
        return None
    best, best_score = None, 0.0
    for needle in needles:
        n = _norm(needle)
        if not n:
            continue
        tokens = [t for t in re.findall(r"[a-z0-9]+", n) if len(t) >= 3]
        for seg in segments:
            st = _norm(seg.get("text", ""))
            if n in st:
                score = 100.0 + len(n)
            elif tokens:
                score = sum(t in st for t in tokens) / len(tokens)
                score = score if score >= 0.5 else 0
            else:
                score = 0
            if score > best_score:
                best, best_score = float(seg.get("start", 0.0)), score
    return best


def _snap(segments: list[dict], s: Any) -> float | None:
    """Clamp a model-given second to the start of the segment that contains it."""
    try:
        s = float(s)
    except (TypeError, ValueError):
        return None
    if not segments:
        return max(0.0, s)
    starts = sorted(float(x.get("start", 0)) for x in segments)
    prior = [x for x in starts if x <= s + 1e-6]
    return prior[-1] if prior else starts[0]


def _fix_evidence(ev: Any, segments: list[dict], *needles: str, n_photos: int = 0) -> dict:
    ev = dict(ev) if isinstance(ev, dict) else {}
    ev["quote"] = str(ev.get("quote") or "")
    src = _snap(segments, ev.get("source_s")) if ev.get("source_s") is not None else None
    # Prefer a textual match when the quote is found; it is more reliable than the model's arithmetic.
    found = _locate(segments, ev["quote"], *needles) if segments else None
    ev["source_s"] = found if found is not None else src
    photo = ev.get("photo")
    ev["photo"] = photo if isinstance(photo, int) and 0 <= photo < n_photos else None
    return ev


def normalize(raw: dict, segments: list[dict], n_photos: int) -> dict:
    """Best-effort deterministic cleanup of the model JSON before pydantic validation."""
    d = dict(raw) if isinstance(raw, dict) else {}
    # Products
    products, ids = [], set()
    for p in d.get("products") or []:
        if not isinstance(p, dict):
            continue
        p = dict(p)
        p["name"] = str(p.get("name") or "").strip()
        if not p["name"]:
            continue
        pid = slugify(str(p.get("id") or p["name"]))
        base, i = pid, 2
        while pid in ids:
            pid, i = f"{base}-{i}", i + 1
        ids.add(pid)
        p["id"] = pid
        p["price_cop"] = parse_cop(p.get("price_cop", p.get("price")))
        p.pop("price", None)
        p["description"] = str(p.get("description") or "")
        photo = p.get("photo")
        p["photo"] = photo if isinstance(photo, int) and 0 <= photo < n_photos else None
        p["evidence"] = _fix_evidence(p.get("evidence"), segments, p["name"], n_photos=n_photos)
        if p["evidence"]["photo"] is None and p["photo"] is not None:
            p["evidence"]["photo"] = p["photo"]
        products.append(p)
    d["products"] = products

    # Hours
    h = d.get("hours")
    if isinstance(h, dict):
        open_ = parse_hhmm(h.get("open"))
        close = parse_hhmm(h.get("close"))
        if open_ and close:
            o, c = int(open_[:2]), int(close[:2])
            if c <= o and c < 12 and c + 12 > o:  # "de 11 a 2" -> 11:00-14:00
                close = f"{c + 12:02d}{close[2:]}"
            days = _norm_days(h.get("days")) or _WEEK[:6]
            d["hours"] = {"days": days, "open": open_, "close": close,
                          "evidence": _fix_evidence(h.get("evidence"), segments, "abro", "horario", n_photos=n_photos)}
        else:
            d["hours"] = None
    elif h is not None:
        d["hours"] = None

    # Enums
    f = _norm(d.get("fulfillment") or "")
    d["fulfillment"] = {"pickup": "pickup", "delivery": "delivery", "both": "both", "recogida": "pickup",
                        "domicilio": "delivery", "ambos": "both"}.get(f, "pickup")
    if not f and "fulfillment" not in (d.get("missing") or []):
        d.setdefault("missing", []).append("fulfillment")
    pm_map = {"cash": "cash", "efectivo": "cash", "link": "link", "transfer": "transfer", "nequi": "transfer",
              "daviplata": "transfer", "transferencia": "transfer"}
    pms = []
    for m in d.get("payment_methods") or []:
        v = pm_map.get(_norm(m))
        if v and v not in pms:
            pms.append(v)
    if pms:
        d["payment_methods"] = pms
    else:
        d.pop("payment_methods", None)

    bc = d.get("blocked_category")
    d["blocked_category"] = bc if bc in BLOCKED_CATEGORIES else None

    # Strings and limits
    for k in ("business_name", "owner_name", "category", "city", "neighborhood", "address_hint", "delivery_zone",
              "tone", "about", "description", "clarifying_question"):
        if k in d and d[k] is None:
            d.pop(k)
        elif k in d:
            d[k] = str(d[k]).strip()
    if not d.get("category"):
        d["category"] = (products[0]["name"].split()[0].lower() if products else "negocio")
    if not d.get("business_name"):
        word = d["category"].strip().capitalize()
        d["business_name"] = f"{word} {d.get('owner_name', '')}".strip()
    if d.get("about"):
        d["about"] = d["about"][:139]
    if d.get("description"):
        d["description"] = d["description"][:512]
    if not d.get("tone"):
        d.pop("tone", None)
    d["missing"] = [str(x) for x in d.get("missing") or []]

    # Top-level evidence
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    needles = {"business_name": [d.get("business_name", "")], "owner_name": [f"soy {d.get('owner_name', '')}"],
               "neighborhood": [d.get("neighborhood", "")], "city": [d.get("city", "")], "category": [d.get("category", "")]}
    out_ev = {}
    for key, val in ev.items():
        out_ev[str(key)] = _fix_evidence(val, segments, *needles.get(key, []), n_photos=n_photos)
    for key, ns in needles.items():
        if key not in out_ev and any(ns) and segments:
            src = _locate(segments, *ns)
            if src is not None:
                out_ev[key] = {"source_s": src, "photo": None, "quote": ""}
    d["evidence"] = out_ev
    return d


# ---------------------------------------------------------------- essentials and question


def _essential_question(spec: BusinessSpec) -> str:
    unpriced = [p.name for p in spec.products if p.price_cop <= 0]
    if unpriced:
        names = " y ".join(unpriced[:3])
        return f"¿A cómo vendes {names}?"
    if "fulfillment" in spec.missing:
        return "¿Tus clientes recogen el pedido en el puesto, o también haces domicilios?"
    return ""


def _finish(spec: BusinessSpec, after_answer: bool) -> tuple[BusinessSpec, str]:
    """Defaults for presentation fields + the single clarifying question."""
    updates: dict[str, Any] = {}
    if after_answer:
        priced = [p for p in spec.products if p.price_cop > 0]
        if priced and len(priced) < len(spec.products):
            updates["products"] = priced
        updates["missing"] = [m for m in spec.missing if m != "fulfillment" and not m.startswith("price:")]
    place = ", ".join(x for x in (spec.neighborhood, spec.city) if x)
    if not spec.about:
        updates["about"] = (f"{spec.category.capitalize()} en {place}. Pide por aquí." if place else
                            f"{spec.category.capitalize()}. Pide por aquí.")[:139]
    if not spec.description:
        menu = ", ".join(p.name for p in spec.products[:5])
        updates["description"] = f"{spec.business_name}: {menu}. {('En ' + place + '. ') if place else ''}Pide por WhatsApp."[:512]
    if updates:
        spec = spec.model_copy(update=updates)
    question = "" if after_answer or spec.blocked_category else _essential_question(spec)
    if question != spec.clarifying_question:
        spec = spec.model_copy(update={"clarifying_question": question})
    return spec, question


# ---------------------------------------------------------------- public API


def _empty_transcript():
    return _muse().Transcript(text="", segments=[], model="none")


def compile_spec(audio: bytes, mime: str, photos: list[bytes], answer: str = "", previous: Draft | None = None) -> Draft:
    muse = _muse()
    photos = list(photos or [])[:3]
    if previous is not None:
        transcript = previous.transcript
    elif audio:
        transcript = muse.transcribe(audio, mime)
    else:
        transcript = _empty_transcript()
    segments = list(getattr(transcript, "segments", None) or [])

    prompt = _build_prompt(transcript, len(photos), answer, previous)
    schema = spec_mod.json_schema()

    def call(p: str) -> dict:
        return muse.structured(p, schema, "business_spec", system=SYSTEM, images=photos or None,
                               model=settings.model_spec, reasoning=settings.model_reasoning)

    raw = call(prompt)
    try:
        spec = BusinessSpec.model_validate(normalize(raw, segments, len(photos)))
    except ValidationError as err:
        retry_prompt = (
            f"{prompt}\n\nTu respuesta anterior no pasó la validación del esquema:\n{err}\n\n"
            f"Respuesta anterior:\n{json.dumps(raw, ensure_ascii=False)[:4000]}\n\n"
            "Corrígela y devuelve el JSON completo y válido."
        )
        raw = call(retry_prompt)
        try:
            spec = BusinessSpec.model_validate(normalize(raw, segments, len(photos)))
        except ValidationError as err2:
            raise CompileError(f"BusinessSpec invalid after retry: {err2}") from err2

    blocked = spec.blocked_category or detect_blocked(spec.model_dump())
    if blocked and blocked != spec.blocked_category:
        spec = spec.model_copy(update={"blocked_category": blocked})

    spec, question = _finish(spec, after_answer=previous is not None)
    return Draft(spec=spec, transcript=transcript, question=question, blocked=blocked)


def blocked_message(key: str) -> str:
    """Polite refusal for a blocked category (Colombian Spanish)."""
    what = BLOCKED_CATEGORIES.get(key, "esa categoría")
    return (f"Uy, qué pena contigo: por ahora Génesis no puede crear negocios de {what}, "
            "por las políticas de WhatsApp. Si vendes otra cosa, mándame otra nota de voz y lo armamos.")


# ---------------------------------------------------------------- read-back


def _cop(n: int) -> str:
    return "$" + f"{int(n):,}".replace(",", ".")


def _friendly(hhmm: str) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    suffix = "a. m." if h < 12 else "p. m."
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}" if m else f"{h12} {suffix}"


def _days_text(days: list[str]) -> str:
    idx = sorted(_WEEK.index(d) for d in days if d in _WEEK)
    if not idx:
        return ""
    if len(idx) == 7:
        return "Todos los días"
    if len(idx) > 2 and idx == list(range(idx[0], idx[-1] + 1)):
        return f"{_DAY_ES[_WEEK[idx[0]]].capitalize()} a {_DAY_ES[_WEEK[idx[-1]]]}"
    return ", ".join(_DAY_ES[_WEEK[i]] for i in idx).capitalize()


_FULFILLMENT_TEXT = {
    Fulfillment.pickup: "🛍️ Recogen en el puesto",
    Fulfillment.delivery: "🛵 Solo domicilios",
    Fulfillment.both: "🛍️ Recogen en el puesto o 🛵 domicilio",
}
_PAY_TEXT = {PaymentMethod.cash: "efectivo", PaymentMethod.link: "link de pago", PaymentMethod.transfer: "transferencia"}


def summary(spec: BusinessSpec) -> str:
    """WhatsApp-ready read-back; ends asking to confirm with 'sí'."""
    hi = f"¡Listo, {spec.owner_name}! " if spec.owner_name else "¡Listo! "
    lines = [hi + "Así quedaría tu negocio:", "", f"*{spec.business_name}*"]
    place = ", ".join(x for x in (spec.neighborhood, spec.city) if x)
    if place:
        lines.append(f"📍 {place}")
    if spec.hours:
        days = _days_text(spec.hours.days)
        lines.append(f"🕕 {days + ', ' if days else ''}{_friendly(spec.hours.open)} a {_friendly(spec.hours.close)}")
    lines += ["", "*Menú*"]
    lines += [f"• {p.name}: {_cop(p.price_cop)}" for p in spec.products]
    lines.append("")
    ful = _FULFILLMENT_TEXT.get(spec.fulfillment, "")
    if spec.fulfillment != Fulfillment.pickup and spec.delivery_zone:
        ful += f" ({spec.delivery_zone})"
    lines.append(ful)
    if spec.payment_methods:
        pays = [_PAY_TEXT[m] for m in spec.payment_methods]
        txt = pays[0] if len(pays) == 1 else ", ".join(pays[:-1]) + " o " + pays[-1]
        lines.append(f"💵 Pagan con {txt}")
    lines += ["", "¿Está todo bien? Respóndeme *sí* y lo creo, o dime qué le cambio."]
    return "\n".join(lines)


_YES = {
    "si", "sii", "siii", "sip", "sisas", "sisa", "si senor", "si senora", "si claro", "claro", "claro que si",
    "dale", "dale pues", "listo", "de una", "ok", "okay", "okey", "oki", "va", "vale", "correcto", "perfecto",
    "confirmo", "confirmado", "eso es", "asi es", "hagale", "hagale pues", "si dale", "si listo", "si de una",
    "todo bien", "esta bien", "si esta bien", "si todo bien", "yes", "y", "s", "listo dale", "dale listo",
    "si perfecto", "perfecto dale", "bueno", "si senor dale", "de una dale", "si de una dale",
}
_YES_EMOJI = ("👍", "👌", "✅", "🙌", "💯", "✔")
_NEG = re.compile(r"\b(no|pero|cambia|cambiar|cambie|falta|mal|corrige|corregir|espera)\b")


def is_yes(text: str) -> bool:
    if not text:
        return False
    raw = text.strip()
    if any(raw.startswith(e) for e in _YES_EMOJI) and not _NEG.search(_norm(raw)):
        return True
    t = _norm(raw)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)  # "siiiii" -> "sii"
    t = re.sub(r"\s+", " ", t).strip()
    if not t or _NEG.search(t):
        return False
    if t in _YES:
        return True
    # "si, gracias", "dale, perfecto mija": a yes word first, short message, no negation.
    words = t.split()
    return len(words) <= 5 and (words[0] in _YES or " ".join(words[:2]) in _YES)

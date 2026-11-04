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
            "Especificación anterior (consérvala completa: todos sus productos sin repetirlos, dueña, barrio y horario; "
            "solo corrige o completa lo que responde el vendedor):",
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


# Sentence-level segments (<= this long) are precise enough: evidence points at the segment start.
# Real Muse Voice Transcribe returns coarse turns (7-20 s); inside those we interpolate by character offset.
COARSE_SEGMENT_S = 6.0


def _at(seg: dict, pos: int, length: int) -> float:
    """Second of character `pos` inside a segment: its start, or a linear interpolation in a coarse turn."""
    start = float(seg.get("start", 0.0))
    end = float(seg.get("end", start) or start)
    if end - start <= COARSE_SEGMENT_S or length <= 0:
        return start
    return round(start + (end - start) * min(max(pos, 0), length) / length, 1)


def _locate(segments: list[dict], *needles: str) -> float | None:
    """Second where the needles (quote or name) were said: segment start, or interpolated in a coarse turn."""
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
            pos = st.find(n)
            if pos >= 0:
                score = 100.0 + len(n)
            elif tokens:
                hits = [t for t in tokens if t in st]
                score = len(hits) / len(tokens)
                score = score if score >= 0.5 else 0
                if score:
                    m = re.search(r"\b" + re.escape(hits[0]), st)
                    pos = m.start() if m else st.find(hits[0])
            else:
                score = 0
            if score > best_score:
                best, best_score = _at(seg, pos, len(st)), score
    return best


def _snap(segments: list[dict], s: Any) -> float | None:
    """Clamp a model-given second to its segment: the start of a fine segment, or inside a coarse turn."""
    try:
        s = float(s)
    except (TypeError, ValueError):
        return None
    if not segments:
        return max(0.0, s)
    segs = sorted(segments, key=lambda x: float(x.get("start", 0)))
    prior = [x for x in segs if float(x.get("start", 0)) <= s + 1e-6]
    seg = prior[-1] if prior else segs[0]
    start = float(seg.get("start", 0))
    end = float(seg.get("end", start) or start)
    if end - start <= COARSE_SEGMENT_S:
        return start
    return round(min(max(s, start), end), 1)


def _fix_evidence(ev: Any, segments: list[dict], *needles: str, n_photos: int = 0, needles_first: bool = False) -> dict:
    ev = dict(ev) if isinstance(ev, dict) else {}
    ev["quote"] = str(ev.get("quote") or "")
    src = _snap(segments, ev.get("source_s")) if ev.get("source_s") is not None else None
    # Prefer a textual match when the quote is found; it is more reliable than the model's arithmetic.
    # Products use their own name first: one quote often lists several products ("tengo X y Y").
    found = None
    if segments:
        if needles_first:
            found = _locate(segments, *needles)
        if found is None:
            found = _locate(segments, ev["quote"], *needles)
    ev["source_s"] = found if found is not None else src
    photo = ev.get("photo")
    ev["photo"] = photo if isinstance(photo, int) and 0 <= photo < n_photos else None
    return ev


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Per-character _norm (accents off, lowercase) plus the original index of every output character."""
    chars, idx = [], []
    for i, ch in enumerate(str(text)):
        for c in unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode().lower():
            chars.append(c)
            idx.append(i)
    return "".join(chars), idx


_CAP = r"[A-ZÁÉÍÓÚÑ][a-záéíóúñü]+"


def _backfill_from_transcript(d: dict, text: str, segments: list[dict]) -> None:
    """Fill what the model dropped but the seller clearly said (real models sometimes return a thin spec).

    Only values literally present in the transcript are used: owner ("soy Rosa"), neighborhood ("aquí en
    San Fernando"), a shared price ("cada una a cinco mil") and product phrases that start with the
    category word ("arepa de carne desmechada").
    """
    if not text:
        return
    if not d.get("owner_name"):
        m = re.search(r"\b(?:[Ss]oy|me llamo|[Mm]i nombre es)\s+(?:la |el |doña |don )?(" + _CAP + ")", text)
        if m:
            d["owner_name"] = m.group(1)
    if not d.get("neighborhood"):
        m = re.search(r"\b(?:[Bb]arrio|aqu[ií] en|ac[aá] en|[Qq]uedo en|[Ee]stoy en)\s+(?:el |la )?(" + _CAP + r"(?:\s+(?:de\s+)?" + _CAP + ")*)", text)
        if m and _norm(m.group(1)) != _norm(d.get("city") or "Cali"):
            d["neighborhood"] = m.group(1)
    t = _norm(text)
    shared = 0
    m = re.search(r"\bcada (?:una|uno)\s+(?:a|en|vale|cuesta|sale)\s+([^,.;]+)", t)
    if m:
        shared = parse_cop(m.group(1))
    products = d.get("products") or []
    if not products:  # an empty answer is a failed call: let the validation retry ask the model again
        return
    cat = _norm(d.get("category") or (products[0]["name"].split()[0] if products else ""))
    sing = re.sub(r"(es|s)$", "", cat.split()[0]) if cat else ""
    if len(sing) < 3:
        return
    have = [_tokens(p["name"]) for p in products]
    ids = {p["id"] for p in products}
    for frag in re.split(r"[,.;:!?]", t):
        for piece in re.split(r"\s+(?:y|e|o)\s+(?=" + re.escape(sing) + r"s?\b)", frag):
            m = re.search(r"\b" + re.escape(sing) + r"s?\s+(?:de|con)\s+[a-z ]+", piece)
            if not m:
                continue
            name = re.split(r"\s+(?:cada|a|por|que|para|en|y)\s", m.group(0).strip() + " ")[0].strip()
            words = name.split()
            if len(words) < 3 or len(words) > 7:
                continue
            toks = _tokens(name)
            if any(toks <= h or h <= toks for h in have if h):
                continue
            pid = slugify(name)
            if pid in ids:
                continue
            ids.add(pid)
            have.append(toks)
            products.append({"id": pid, "name": name[0].upper() + name[1:], "description": "", "price_cop": shared,
                             "photo": None, "evidence": _fix_evidence({}, segments, name, needles_first=True)})
            if not shared:
                d.setdefault("missing", []).append(f"price:{pid}")
            d["description"] = ""  # the model's text missed this product: _finish rewrites it from the menu
    d["products"] = products


def _name_key(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _norm(name)))


_TIME = (r"(\d{1,2}(?::\d{2})?|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)"
         r"((?:\s+y\s+media)?(?:\s*(?:de|en|por)\s+la\s+(?:manana|tarde|noche)|\s*(?:a\.?\s?m\.?|p\.?\s?m\.?)(?![a-z]))?)")
_RANGE = re.compile(r"\b(?:de|desde)\s+(?:las\s+)?" + _TIME + r"\s+(?:a|hasta)\s+(?:las\s+)?" + _TIME)
_DAY_WORD = r"(lunes|martes|miercoles|jueves|viernes|sabados?|domingos?)"


def extract_hours(text: str) -> dict | None:
    """Deterministic backstop: 'abro de lunes a sábado de seis a once de la mañana' -> days + open/close."""
    t, idx = _norm_map(text)
    for m in _RANGE.finditer(t):
        o_num, o_sfx, c_num, c_sfx = m.groups()
        close = parse_hhmm(c_num + c_sfx)
        open_ = parse_hhmm(o_num + o_sfx)
        if not open_ or not close:
            continue
        if not re.search(r"manana|tarde|noche|m\b", o_sfx) and re.search(r"manana|tarde|noche|m\b", c_sfx):
            shared = parse_hhmm(o_num + " " + c_sfx.replace("y media", "").strip())  # "de 2 a 8 de la noche"
            if shared and shared < close:
                open_ = shared[:3] + open_[3:]
        o, c = int(open_[:2]), int(close[:2])
        if c <= o and c < 12 and c + 12 > o:
            close = f"{c + 12:02d}{close[2:]}"
        if close <= open_:
            continue
        window = t[max(0, m.start() - 60): m.end() + 60]
        days: list[str] = []
        if re.search(r"todos los dias|toda la semana|diario", window):
            days = list(_WEEK)
        else:
            rng = re.search(r"\b(?:de\s+)?" + _DAY_WORD + r"\s+a\s+" + _DAY_WORD, window)
            if rng:
                a, b = (_DAYS[x[:-1] if x.endswith("os") else x] for x in rng.groups())
                i, j = _WEEK.index(a), _WEEK.index(b)
                days = _WEEK[i: j + 1] if i <= j else _WEEK[i:] + _WEEK[: j + 1]
            else:
                days = _norm_days([x[:-1] if x.endswith("os") else x for x in re.findall(_DAY_WORD, window)])
        quote_start = t.rfind("abro", 0, m.start())
        a = quote_start if 0 <= quote_start and m.start() - quote_start < 60 else m.start()
        quote = str(text)[idx[a]: idx[m.end() - 1] + 1]  # original words, with accents
        return {"days": days or _WEEK[:6], "open": open_, "close": close, "quote": quote}
    return None


def normalize(raw: dict, segments: list[dict], n_photos: int, text: str = "") -> dict:
    """Best-effort deterministic cleanup of the model JSON before pydantic validation."""
    d = dict(raw) if isinstance(raw, dict) else {}
    # Products
    products, ids, by_name = [], set(), {}
    for p in d.get("products") or []:
        if not isinstance(p, dict):
            continue
        p = dict(p)
        p["name"] = str(p.get("name") or "").strip()
        if len(re.findall(r"[a-z]", _norm(p["name"]))) < 3:  # "x", "", "?": placeholders, not products
            continue
        key = _name_key(p["name"])
        if key in by_name:  # the model repeated a product: keep the first, fill its price if it had none
            first = by_name[key]
            if first["price_cop"] <= 0:
                first["price_cop"] = parse_cop(p.get("price_cop", p.get("price")))
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
        p["evidence"] = _fix_evidence(p.get("evidence"), segments, p["name"], n_photos=n_photos, needles_first=True)
        if p["evidence"]["photo"] is None and p["photo"] is not None:
            p["evidence"]["photo"] = p["photo"]
        products.append(p)
        by_name[key] = p
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
    if d.get("hours") is None:  # the model sometimes leaves hours null although they were said
        said = text or " ".join(str(s.get("text", "")) for s in segments)
        found = extract_hours(said)
        if found:
            ev = _fix_evidence({"quote": found.pop("quote")}, segments, "abro", "horario", n_photos=n_photos)
            d["hours"] = {**found, "evidence": ev}
            d["missing"] = [m for m in d.get("missing") or [] if m != "hours"]

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

    _backfill_from_transcript(d, text, segments)
    pids = {p["id"] for p in d["products"]}
    d["missing"] = [m for m in d["missing"] if not m.startswith("price:") or m[6:] in pids]

    # Top-level evidence
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    needles = {"business_name": [d.get("business_name", "")], "owner_name": [f"soy {d.get('owner_name', '')}"],
               "neighborhood": [d.get("neighborhood", "")], "city": [d.get("city", "")], "category": [d.get("category", "")],
               "payment_methods": [w for m in d.get("payment_methods") or []
                                   for w in {"cash": ["efectivo"], "link": ["link"], "transfer": ["nequi", "transferencia"]}[m]],
               "fulfillment": ["domicilio", "recogen", "recoger", "en el puesto"]}
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


# ---------------------------------------------------------------- answer merge

_STOP = {"con", "del", "las", "los", "para", "por", "una", "uno"}


def _tokens(name: str) -> set[str]:
    return {t for t in _name_key(name).split() if len(t) >= 3 and t not in _STOP}


def _mentions(answer_norm: str, name: str, common: set[str]) -> bool:
    """The answer names this product by a distinctive word ('la de carne a 6' -> 'Arepa de carne desmechada')."""
    words = set(re.findall(r"[a-z0-9]+", answer_norm))
    return bool((_tokens(name) - common) & words)


def _fulfillment_from(answer_norm: str) -> Fulfillment | None:
    a = answer_norm
    if re.search(r"\b(no|ni) (hago|hacemos|tengo|tenemos|manejo|manejamos|hay|mando|llevo)( los| el)? domicilios?|sin domicilios?|solo (para )?recog|solo en el puesto", a):
        return Fulfillment.pickup
    if re.search(r"\bsolo (a |por |con )?domicilios?|no (pueden|se puede) recoger", a):
        return Fulfillment.delivery
    if re.search(r"\b(tambien|y) (hago |hacemos |tengo |manejo )?domicilios?|las dos|ambas|ambos|de las dos", a):
        return Fulfillment.both
    return None


_PAY_WORDS = r"\b(efectivo|nequi|daviplata|bancolombia|transfer|link|tarjeta|pagan|pago)"


def merge_answer(prev: BusinessSpec, new: BusinessSpec, answer: str) -> BusinessSpec:
    """The seller's answer can only ADD what was missing or change what it clearly names.

    The model re-emits the whole spec after an answer and, with real models, sometimes drops or
    duplicates fields (3x the same product, owner and neighborhood gone). So the previous draft is
    the base and only these values come from the new output: fields listed in `missing`, empty
    fields the answer fills, values the answer mentions verbatim, prices for unpriced products and
    products the answer names.
    """
    a = _norm(answer)
    missing = set(prev.missing)
    out = prev.model_dump()
    nd = new.model_dump()
    taken: set[str] = set()

    def take(key: str) -> None:
        out[key] = nd[key]
        taken.add(key)
        if key in nd["evidence"]:
            out["evidence"][key] = nd["evidence"][key]

    for key in ("business_name", "owner_name", "category", "city", "neighborhood", "address_hint"):
        val = str(nd.get(key) or "").strip()
        if not val or val == out.get(key):
            continue
        if key in missing or (not out.get(key) and _norm(val) in a) or (len(val) >= 3 and _norm(val) in a):
            take(key)

    ful = _fulfillment_from(a)
    if ful is not None:
        out["fulfillment"] = ful.value
        out["evidence"]["fulfillment"] = {"source_s": None, "photo": None, "quote": answer.strip()[:200]}
        taken.add("fulfillment")
    elif "fulfillment" in missing or re.search(r"domicilio|recog|puesto|entrega|envio", a):
        take("fulfillment")
    if out["fulfillment"] != "pickup" and nd.get("delivery_zone") and (not out.get("delivery_zone") or "fulfillment" in taken):
        take("delivery_zone")

    if "payment_methods" in missing or re.search(_PAY_WORDS, a):
        take("payment_methods")

    said = extract_hours(answer) if answer else None
    if said:
        quote = said.pop("quote")
        out["hours"] = {**said, "evidence": {"source_s": None, "photo": None, "quote": quote}}
        taken.add("hours")
    elif nd.get("hours") and (out.get("hours") is None or "hours" in missing):
        take("hours")

    # Products: previous list is the base, matched by normalized name (then id, then word overlap).
    prev_products = [dict(p) for p in out["products"]]
    names = [p["name"] for p in prev_products] + [p["name"] for p in nd["products"]]
    counts: dict[str, int] = {}
    for n in names:
        for t in _tokens(n):
            counts[t] = counts.get(t, 0) + 1
    common = {t for t, c in counts.items() if c >= 2} | _tokens(out.get("category", ""))
    ids = {p["id"] for p in prev_products}
    added = False
    for np_ in nd["products"]:
        key = _name_key(np_["name"])
        match = next((p for p in prev_products if _name_key(p["name"]) == key), None)
        match = match or next((p for p in prev_products if p["id"] == np_["id"]), None)
        if match is None:
            nt = _tokens(np_["name"])
            best = max(prev_products, key=lambda p: len(nt & _tokens(p["name"])) / max(1, len(nt | _tokens(p["name"]))), default=None)
            if best is not None and best["price_cop"] <= 0 and nt and len(nt & _tokens(best["name"])) / len(nt | _tokens(best["name"])) >= 0.5:
                match = best
        if match is not None:
            price = int(np_.get("price_cop") or 0)
            if price > 0 and (match["price_cop"] <= 0 or (price != match["price_cop"] and _mentions(a, match["name"], common))):
                match["price_cop"] = price
            if match.get("photo") is None and np_.get("photo") is not None:
                match["photo"] = np_["photo"]
        elif _mentions(a, np_["name"], common) and _name_key(np_["name"]) not in {_name_key(p["name"]) for p in prev_products}:
            p = dict(np_)
            pid, i = p["id"], 2
            while pid in ids:
                pid, i = f"{np_['id']}-{i}", i + 1
            p["id"] = pid
            ids.add(pid)
            prev_products.append(p)
            added = True
    out["products"] = prev_products
    if added:
        out["description"] = ""  # regenerated from the new menu by _finish

    if nd.get("blocked_category") and not out.get("blocked_category"):
        out["blocked_category"] = nd["blocked_category"]
    priced = {f"price:{p['id']}" for p in prev_products if p["price_cop"] > 0}
    out["missing"] = [m for m in out["missing"] if m not in taken and m not in priced
                      and not (m == "hours" and out.get("hours"))]
    out["clarifying_question"] = ""
    return BusinessSpec.model_validate(out)


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

    text = str(getattr(transcript, "text", "") or "")
    raw = call(prompt)
    try:
        spec = BusinessSpec.model_validate(normalize(raw, segments, len(photos), text))
    except ValidationError as err:
        retry_prompt = (
            f"{prompt}\n\nTu respuesta anterior no pasó la validación del esquema:\n{err}\n\n"
            f"Respuesta anterior:\n{json.dumps(raw, ensure_ascii=False)[:4000]}\n\n"
            "Corrígela y devuelve el JSON completo y válido."
        )
        raw = call(retry_prompt)
        try:
            spec = BusinessSpec.model_validate(normalize(raw, segments, len(photos), text))
        except ValidationError as err2:
            if previous is None:
                raise CompileError(f"BusinessSpec invalid after retry: {err2}") from err2
            spec = previous.spec  # the answer can still patch it deterministically below

    if previous is not None:
        spec = merge_answer(previous.spec, spec, answer)

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

"""Meta Model API client (OpenAI-compatible) with a deterministic offline mock.

Endpoints (https://dev.meta.ai/docs):
- chat.completions (Muse Spark 1.3 / 1.2) through the `openai` SDK, reasoning via `reasoning_effort`.
- POST /v1/asr/transcribe (Muse Voice Transcribe): multipart `request` (JSON) + `audio` (WAV mono PCM 16 kHz).
  Response: {"sessionId", "transcript", "audioDurationMs", "turns": [{"turnId","startMs","endMs","transcript"}]}.
- POST /v1/responses with model sam-3.1: output text made of special tokens,
  `<0f>0<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..[;c=..]|><|mask;...|>,1<|box;...|>...` (pixels, xyxy).
- /v1/images/generations and /v1/images/edits (Muse Image): data[].b64_json or data[].url.

Every public function has a mock branch (`settings.mock`) built on the Rosa fixtures in `fixtures/`.
"""

from __future__ import annotations

import base64
import io
import json
import re
import struct
import subprocess
import tempfile
import time
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import settings

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TIMEOUT = httpx.Timeout(90.0, connect=10.0)


class MuseError(RuntimeError):
    pass


@dataclass
class Transcript:
    text: str
    segments: list[dict]  # [{"start": float, "end": float, "text": str}], may be [] if API gives none
    model: str


# --------------------------------------------------------------------------------------------- utils


def _headers(json_body: bool = True) -> dict[str, str]:
    h = {"Authorization": f"Bearer {settings.model_api_key}", "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _url(path: str) -> str:
    return settings.model_base_url.rstrip("/") + "/" + path.lstrip("/")


def _client():
    from openai import OpenAI

    return OpenAI(api_key=settings.model_api_key or "missing", base_url=settings.model_base_url, timeout=90.0, max_retries=2)


def _post_json(path: str, body: dict) -> dict:
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.post(_url(path), headers=_headers(), json=body)
    if r.status_code >= 400:
        raise MuseError(f"{path} {r.status_code}: {r.text[:500]}")
    return r.json()


def sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def data_url(data: bytes, mime: str | None = None) -> str:
    return f"data:{mime or sniff_mime(data)};base64,{base64.b64encode(data).decode()}"


def image_size(data: bytes) -> tuple[int, int]:
    """(width, height) of PNG/JPEG bytes; Pillow if available, PNG header otherwise."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        pass
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return 512, 512


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if m:
        return m.group(1)
    if not t.startswith(("{", "[")):
        start = t.find("{")
        end = t.rfind("}")
        if start != -1 and end > start:
            return t[start : end + 1]
    return t


def _content_text(content: Any) -> str | None:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") or "" for p in content]
        return "".join(parts)
    return str(content)


def _reasoning_extra(reasoning: str | None) -> dict:
    effort = reasoning or settings.model_reasoning
    return {"reasoning_effort": effort} if effort else {}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------- transcription


def to_wav16k(audio: bytes, mime: str = "audio/ogg") -> bytes:
    """WhatsApp voice notes are OGG/Opus; ASR wants WAV mono 16 kHz s16le. Uses temp files so the WAV
    header carries real sizes (piped WAV output has an unknown length the API may reject)."""
    suffix = {"audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
              "audio/x-wav": ".wav", "audio/webm": ".webm", "audio/amr": ".amr"}.get(mime.split(";")[0].strip(), ".bin")
    with tempfile.TemporaryDirectory(prefix="genesis-asr-") as d:
        src, dst = Path(d) / f"in{suffix}", Path(d) / "out.wav"
        src.write_bytes(audio)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", "-map_metadata", "-1", str(dst)]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0 or not dst.exists():
            raise MuseError(f"ffmpeg failed: {proc.stderr.decode(errors='ignore')[:300]}")
        return dst.read_bytes()


def asr_raw(wav: bytes, mode: str = "ENDPOINTING") -> dict:
    """POST /v1/asr/transcribe. Returns the raw TranscribeResponse JSON."""
    req = {"model": settings.model_transcribe, "audioEncoding": "WAV", "mode": mode, "languageBias": ["Spanish"]}
    files = {
        "request": (None, json.dumps(req), "application/json"),
        "audio": ("voice.wav", wav, "audio/wav"),
    }
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.post(_url("asr/transcribe"), headers=_headers(json_body=False), files=files)
    if r.status_code >= 400:
        raise MuseError(f"asr/transcribe {r.status_code}: {r.text[:500]}")
    return r.json()


def parse_asr(raw: dict) -> Transcript:
    turns = raw.get("turns") or []
    segments = [
        {"start": round(int(t.get("startMs", 0)) / 1000, 2), "end": round(int(t.get("endMs", 0)) / 1000, 2),
         "text": (t.get("transcript") or "").strip()}
        for t in sorted(turns, key=lambda t: t.get("turnId", 0))
        if (t.get("transcript") or "").strip()
    ]
    text = (raw.get("transcript") or raw.get("text") or " ".join(s["text"] for s in segments)).strip()
    return Transcript(text=text, segments=segments, model=settings.model_transcribe)


def transcribe(audio: bytes, mime: str = "audio/ogg") -> Transcript:
    if settings.mock:
        fx = load_fixture("rosa_transcript.json")
        return Transcript(text=fx["text"], segments=fx["segments"], model=fx.get("model", settings.model_transcribe))
    wav = to_wav16k(audio, mime)
    errors: list[str] = []
    for mode in ("ENDPOINTING", "PUSH_TO_TALK"):  # ENDPOINTING returns timed turns; PTT is the default mode
        try:
            t = parse_asr(asr_raw(wav, mode))
            if t.text:
                return t
            errors.append(f"{mode}: empty transcript")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{mode}: {exc}")
    # Fallback: Spark (1.2 is better for audio) with an input_audio part.
    try:
        msg = [{"role": "user", "content": [
            {"type": "text", "text": "Transcribe literalmente esta nota de voz en español. Devuelve solo el texto."},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}},
        ]}]
        resp = _client().chat.completions.create(model=settings.model_audio, messages=msg)
        text = (_content_text(resp.choices[0].message.content) or "").strip()
        if text:
            return Transcript(text=text, segments=[], model=settings.model_audio)
        errors.append("chat fallback: empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"chat fallback: {exc}")
    raise MuseError("transcription failed: " + " | ".join(errors))


# ----------------------------------------------------------------------------------- structured output

# Tests and other modules may register mock answers by schema name: MOCK_STRUCTURED["flow"] = {...}
MOCK_STRUCTURED: dict[str, Any] = {}


def _image_parts(images: list[bytes] | None) -> list[dict]:
    return [{"type": "image_url", "image_url": {"url": data_url(img)}} for img in images or []]


def structured(prompt: str, schema: dict, name: str, *, system: str = "", images: list[bytes] | None = None,
               model: str | None = None, reasoning: str | None = None) -> dict:
    if settings.mock:
        return _mock_structured(prompt, schema, name, images)
    content: Any = prompt if not images else [{"type": "text", "text": prompt}, *_image_parts(images)]
    messages: list[dict] = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    rf = {"type": "json_schema", "json_schema": {"name": re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64] or "out", "schema": schema, "strict": False}}
    client = _client()
    last = ""
    for attempt in range(2):
        resp = client.chat.completions.create(model=model or settings.model_spec, messages=messages, response_format=rf,
                                              extra_body=_reasoning_extra(reasoning))
        last = _content_text(resp.choices[0].message.content) or ""
        try:
            out = json.loads(_strip_fences(last))
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass
        if attempt == 0:
            messages = messages + [
                {"role": "assistant", "content": last},
                {"role": "user", "content": "Tu respuesta no fue un objeto JSON válido. Responde SOLO con el objeto JSON que cumple el esquema."},
            ]
    raise MuseError(f"structured({name}): invalid JSON after retry: {last[:300]}")


def _mock_structured(prompt: str, schema: dict, name: str, images: list[bytes] | None) -> dict:
    if name in MOCK_STRUCTURED:
        val = MOCK_STRUCTURED[name]
        return val(prompt, schema, images) if callable(val) else json.loads(json.dumps(val))
    props = (schema or {}).get("properties", {})
    if "business_name" in props and "products" in props:
        return load_fixture("rosa_spec.json")
    if "boxes" in props:
        w, h = image_size(images[0]) if images else (512, 512)
        return {"boxes": [{"box": [round(w * 0.2), round(h * 0.2), round(w * 0.8), round(h * 0.8)], "score": 0.9}]}
    return _instance_from_schema(schema, schema)


def _instance_from_schema(node: dict, root: dict, depth: int = 0) -> Any:
    """Minimal deterministic instance of a JSON schema (defaults first). Good enough for offline runs."""
    if depth > 8 or not isinstance(node, dict):
        return None
    if "$ref" in node:
        ref = node["$ref"].split("/")[-1]
        return _instance_from_schema(root.get("$defs", {}).get(ref, {}), root, depth + 1)
    if "default" in node:
        return node["default"]
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            opts = [o for o in node[key] if o.get("type") != "null"] or node[key]
            return _instance_from_schema(opts[0], root, depth + 1)
    t = node.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or "properties" in node:
        req = set(node.get("required", node.get("properties", {}).keys()))
        return {k: _instance_from_schema(v, root, depth + 1) for k, v in node.get("properties", {}).items() if k in req}
    if t == "array":
        n = node.get("minItems", 0)
        return [_instance_from_schema(node.get("items", {}), root, depth + 1) for _ in range(n)]
    return {"string": "", "integer": node.get("minimum", 0), "number": node.get("minimum", 0), "boolean": False}.get(t)


# ------------------------------------------------------------------------------------------------ chat


def _as_openai_tool(tool: dict) -> dict:
    if tool.get("type") == "function" and "function" in tool:
        return tool
    return {"type": "function", "function": {k: tool[k] for k in ("name", "description", "parameters") if k in tool}}


def _tool_name(tool: dict) -> str:
    return (tool.get("function") or tool).get("name", "")


def chat(messages: list[dict], tools: list[dict] | None = None, *, model: str | None = None,
         reasoning: str | None = None) -> dict:
    """Returns {"content": str|None, "tool_calls": [{"id","name","arguments": dict}]}."""
    if settings.mock:
        return _mock_chat(messages, tools)
    kwargs: dict[str, Any] = {"model": model or settings.model_spec, "messages": messages, "extra_body": _reasoning_extra(reasoning)}
    if tools:
        kwargs["tools"] = [_as_openai_tool(t) for t in tools]
    resp = _client().chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    calls = []
    for tc in msg.tool_calls or []:
        fn = tc.function
        try:
            args = json.loads(fn.arguments or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.arguments}
        calls.append({"id": tc.id, "name": fn.name, "arguments": args if isinstance(args, dict) else {"value": args}})
    return {"content": _content_text(msg.content), "tool_calls": calls}


# ---- mock chat: deterministic rule-based seller agent for the inspector and offline demo

_MOCK_SPEC: dict | None = None
DAY_ES = {"mon": "lunes", "tue": "martes", "wed": "miércoles", "thu": "jueves", "fri": "viernes", "sat": "sábado", "sun": "domingo"}
_NUM_WORDS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
              "nueve": 9, "diez": 10, "media docena": 6, "docena": 12}
_ALCOHOL = ("cerveza", "pola", "aguardiente", "guaro", "ron ", "licor", "trago", "vino", "alcohol", "whisky", "vodka", "michelada")
_ORDER = ("quiero", "quisiera", "pedir", "pedido", "ordenar", "encargar", "me regala", "me regalas", "me das", "me da ",
          "me manda", "me separa", "apartar", "comprar")


def set_mock_spec(spec: Any | None) -> None:
    """Pin the business the mock agent talks about (BusinessSpec or dict). None → auto-detect / Rosa."""
    global _MOCK_SPEC
    _MOCK_SPEC = None if spec is None else (spec.model_dump(mode="json") if hasattr(spec, "model_dump") else dict(spec))


def _spec_from_messages(messages: list[dict]) -> dict:
    if _MOCK_SPEC is not None:
        return _MOCK_SPEC
    from .spec import BusinessSpec

    for m in messages:
        if m.get("role") != "system":
            continue
        text = _content_text(m.get("content")) or ""
        for match in re.finditer(r"\{", text):  # first embedded JSON object that is a valid BusinessSpec
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[match.start():])
                return BusinessSpec.model_validate(obj).model_dump(mode="json")
            except Exception:  # noqa: BLE001
                continue
    return load_fixture("rosa_spec.json")


def money(cop: int) -> str:
    return "$" + f"{int(cop):,}".replace(",", ".")


def _hhmm(t: str) -> str:
    h, _, m = t.partition(":")
    hi = int(h or 0)
    suffix = "a. m." if hi < 12 else "p. m."
    h12 = hi % 12 or 12
    return f"{h12}:{m or '00'} {suffix}"


def _days(days: list[str]) -> str:
    order = list(DAY_ES)
    idx = sorted(order.index(d) for d in days if d in DAY_ES)
    if not idx:
        return ""
    if idx == list(range(idx[0], idx[-1] + 1)) and len(idx) > 2:
        return f"de {DAY_ES[order[idx[0]]]} a {DAY_ES[order[idx[-1]]]}"
    names = [DAY_ES[order[i]] for i in idx]
    return ", ".join(names[:-1]) + (" y " if len(names) > 1 else "") + names[-1]


def _match_products(text: str, products: list[dict]) -> list[dict]:
    t = _norm(text)
    hits = []
    for p in products:
        name = _norm(p["name"])
        words = [w for w in re.findall(r"[a-z0-9]+", name) if len(w) > 3 and w not in ("arepa", "arepas", "con", "queso")]
        if name in t or (words and any(w in t for w in words)):
            hits.append(p)
    return hits


def _has_qty(text: str) -> bool:
    t = _norm(text)
    return bool(re.search(r"\b\d{1,2}\b", t)) or any(re.search(rf"\b{w}\b", t) for w in _NUM_WORDS if w not in ("un", "una", "uno"))


def _qty(text: str) -> int:
    t = _norm(text)
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        return max(1, int(m.group(1)))
    for word, n in sorted(_NUM_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\b", t):
            return n
    return 1


def _mock_chat(messages: list[dict], tools: list[dict] | None) -> dict:
    spec = _spec_from_messages(messages)
    products = spec.get("products", [])
    owner = spec.get("owner_name") or "el negocio"
    menu = "\n".join(f"• {p['name']}: {money(p['price_cop'])}" for p in products)
    last = messages[-1] if messages else {"role": "user", "content": ""}

    if last.get("role") == "tool":
        data: Any = _content_text(last.get("content")) or ""
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            pass
        if isinstance(data, dict) and (data.get("id") or data.get("order_id")):
            total = data.get("total_cop")
            extra = f" por {money(total)}" if total else ""
            return {"content": f"¡Listo! Tu pedido {data.get('id') or data.get('order_id')} quedó registrado{extra}. "
                               "Ya te mando el link para pagar.", "tool_calls": []}
        return {"content": "¡Listo! Ya quedó. ¿Te ayudo con algo más?", "tool_calls": []}

    text = _content_text(last.get("content")) or ""
    t = _norm(text)
    tool_names = {_tool_name(x) for x in tools or []}

    def reply(s: str) -> dict:
        return {"content": s, "tool_calls": []}

    if any(w in t + " " for w in _ALCOHOL):
        return reply(f"Qué pena, no vendemos bebidas alcohólicas. Lo que sí tenemos es:\n{menu}")

    if any(w in t + " " for w in _ORDER):
        items = _match_products(text, products)
        qty = _qty(text)
        if not items and not _has_qty(text):  # "quiero hacer un pedido": nothing to order yet
            return reply("¡Claro! Toca el botón «Hacer pedido» y eliges cantidades y la hora de recogida.")
        items = items or products[:1]
        if "crear_pedido" in tool_names:
            hours = spec.get("hours") or {}
            args = {
                "items": [{"product_id": p["id"], "name": p["name"], "qty": qty, "unit_cop": p["price_cop"]} for p in items],
                "pickup_time": hours.get("open", ""),
                "customer_name": "Cliente",
            }
            return {"content": None, "tool_calls": [{"id": f"call_mock_{int(time.time() * 1000) % 10**8}", "name": "crear_pedido", "arguments": args}]}
        return reply("¡Claro! Toca el botón «Hacer pedido» y eliges cantidades y la hora de recogida.")

    if re.search(r"precio|cuanto (vale|cuesta|valen|cuestan|es|sale)|a como|valor|cuanto", t):
        hits = _match_products(text, products) or products
        if len(hits) == 1:
            p = hits[0]
            return reply(f"La {p['name'].lower()} vale {money(p['price_cop'])}. ¿Te anoto una?")
        return reply(f"Estos son nuestros precios:\n{menu}")

    if re.search(r"horario|\bhoras?\b|\babre|\bcierra|\batiende|abierto|que dias", t):
        hours = spec.get("hours")
        if not hours:
            return reply(f"Déjame confirmarte el horario con {owner} y ya te aviso.")
        return reply(f"Atendemos {_days(hours['days'])}, de {_hhmm(hours['open'])} a {_hhmm(hours['close'])}.")

    if re.search(r"domicilio|envio|envian|llevan|mandan|recog|retir|donde (estan|quedan)|direccion|ubicacion", t):
        place = ", ".join(x for x in (spec.get("neighborhood"), spec.get("city")) if x)
        f = spec.get("fulfillment", "pickup")
        if f == "pickup":
            return reply(f"Por ahora no hacemos domicilios: pides por aquí y recoges en el puesto en {place}.")
        if f == "delivery":
            zone = spec.get("delivery_zone") or place
            return reply(f"Sí, hacemos domicilios en {zone}.")
        return reply(f"Puedes recoger en {place} o pedir domicilio{(' en ' + spec['delivery_zone']) if spec.get('delivery_zone') else ''}.")

    if re.search(r"pago|pagar|efectivo|nequi|tarjeta|transferencia|link", t):
        names = {"cash": "efectivo", "link": "link de pago (Mercado Pago)", "transfer": "transferencia"}
        methods = [names.get(m, m) for m in spec.get("payment_methods", [])]
        return reply("Recibimos " + " o ".join(methods) + ".")

    if re.search(r"menu|carta|que (venden|tienen|hay)|productos", t):
        return reply(f"Esto es lo que tenemos hoy:\n{menu}\n¿Qué te provoca?")

    if re.search(r"^(hola|buenas|buenos|buen dia|hey|que mas)", t):
        return reply(f"¡Hola! Soy el asistente con IA de {spec.get('business_name', 'nuestro negocio')}. "
                     "¿Quieres ver el menú o hacer un pedido?")

    return reply(f"Eso no lo sé responder yo, soy el asistente con IA. Le paso tu mensaje a {owner} y te escribe pronto.")


# ------------------------------------------------------------------------------------------ SAM 3.1

_BOX_RE = re.compile(r"<\|box;x1=(-?[\d.]+);y1=(-?[\d.]+);x2=(-?[\d.]+);y2=(-?[\d.]+);w=(\d+);h=(\d+)(?:;c=([\d.]+))?\|>")


def parse_sam_text(text: str, size: tuple[int, int] | None = None) -> list[dict]:
    """Parse SAM special-token text into [{"box": [x0,y0,x1,y1] px, "score"}], best first.
    Boxes are in source pixels; if `size` differs from the w/h in the token, rescale to `size`."""
    out = []
    for m in _BOX_RE.finditer(text or ""):
        x1, y1, x2, y2 = (float(m.group(i)) for i in range(1, 5))
        fw, fh = int(m.group(5)), int(m.group(6))
        if size and fw and fh and (fw, fh) != tuple(size):
            sx, sy = size[0] / fw, size[1] / fh
            x1, x2, y1, y2 = x1 * sx, x2 * sx, y1 * sy, y2 * sy
        score = float(m.group(7)) if m.group(7) else 1.0
        out.append({"box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)], "score": score})
    out.sort(key=lambda b: -b["score"])
    return out


def responses_text(raw: dict) -> str:
    """Collect output text from a Responses API object (output_text shortcut or output[].content[].text)."""
    if isinstance(raw.get("output_text"), str):
        return raw["output_text"]
    parts = []
    for item in raw.get("output") or []:
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("text"):
                parts.append(c["text"])
    return "".join(parts)


def sam_raw(image: bytes, prompt: str) -> dict:
    body = {
        "model": settings.model_sam,
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": prompt},
            {"type": "input_image", "image_url": data_url(image)},
        ]}],
        "metadata": {"mask_encoding": "one_bit", "include_confidence": "true"},
        "stream": False,
    }
    return _post_json("responses", body)


def segment(image: bytes, prompt: str) -> list[dict]:
    size = image_size(image)
    if settings.mock:
        w, h = size
        return [{"box": [round(w * 0.18, 1), round(h * 0.15, 1), round(w * 0.82, 1), round(h * 0.88, 1)], "score": 0.93}]
    try:
        raw = sam_raw(image, prompt)
        text = responses_text(raw)
        boxes = parse_sam_text(text, size)
        if boxes or text.strip() == "":
            return boxes  # empty output_text means zero matches
    except Exception:  # noqa: BLE001 - fall back to Spark grounding below
        pass
    try:
        schema = {"type": "object", "properties": {"boxes": {"type": "array", "items": {"type": "object", "properties": {
            "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "score": {"type": "number"}}, "required": ["box", "score"]}}}, "required": ["boxes"]}
        out = structured(
            f"La imagen mide {size[0]}x{size[1]} píxeles. Encuentra cada '{prompt}' y devuelve sus cajas en píxeles "
            "[x0, y0, x1, y1] (esquina superior izquierda y esquina inferior derecha) con un score de 0 a 1. "
            "Si no aparece, devuelve boxes vacío.", schema, "boxes", images=[image])
        res = []
        for b in out.get("boxes") or []:
            box = [float(v) for v in (b.get("box") or [])][:4]
            if len(box) == 4 and box[2] > box[0] and box[3] > box[1]:
                res.append({"box": box, "score": float(b.get("score", 0.5))})
        return sorted(res, key=lambda b: -b["score"])
    except Exception:  # noqa: BLE001
        return []


def crop(image: bytes, box: list[float], pad: float = 0.06) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return image
    try:
        with Image.open(io.BytesIO(image)) as im:
            im.load()
            W, H = im.size
            x0, y0, x1, y1 = (float(v) for v in box[:4])
            px, py = (x1 - x0) * pad, (y1 - y0) * pad
            l, t = max(0, int(x0 - px)), max(0, int(y0 - py))
            r, b = min(W, int(round(x1 + px))), min(H, int(round(y1 + py)))
            if r - l < 2 or b - t < 2:
                return image
            out = im.crop((l, t, r, b))
            if out.mode not in ("RGB", "RGBA"):
                out = out.convert("RGBA" if "A" in out.mode else "RGB")
            buf = io.BytesIO()
            out.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return image


# ------------------------------------------------------------------------------------------- images

_SIZES = {"1:1": "1024x1024", "16:9": "1024x576", "9:16": "576x1024", "4:3": "1024x768", "3:4": "768x1024",
          "4:5": "1024x1280", "3:2": "1024x683", "2:3": "683x1024"}


def _size(size: str) -> str:
    return size if re.fullmatch(r"\d+x\d+", size or "") else _SIZES.get(size, "1024x1024")


def _image_from_response(raw: dict) -> bytes:
    data = (raw.get("data") or [{}])[0]
    if data.get("b64_json"):
        return base64.b64decode(data["b64_json"])
    if data.get("url"):
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as http:
            r = http.get(data["url"])
            r.raise_for_status()
            return r.content
    raise MuseError(f"image response without data: {str(raw)[:300]}")


def _to_png(data: bytes) -> bytes:
    """WhatsApp media and profile photos need PNG/JPEG; convert webp if the API ignores output_format."""
    if sniff_mime(data) in ("image/png", "image/jpeg"):
        return data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            buf = io.BytesIO()
            im.convert("RGBA").save(buf, format="PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


def image_generate(prompt: str, size: str = "1:1") -> bytes:
    if settings.mock:
        w, h = (int(x) for x in _size(size).split("x"))
        scale = 256 / max(w, h)
        return mock_png(max(1, int(w * scale)), max(1, int(h * scale)), (244, 180, 0), (176, 48, 32))
    raw = _post_json("images/generations", {"model": settings.model_image, "prompt": prompt, "n": 1, "size": _size(size),
                                             "response_format": "b64_json", "output_format": "png"})
    return _to_png(_image_from_response(raw))


def image_edit(image: bytes, prompt: str) -> bytes:
    if settings.mock:
        w, h = image_size(image)
        scale = 256 / max(w, h, 1)
        return mock_png(max(1, int(w * scale)), max(1, int(h * scale)), (250, 246, 238), (214, 150, 40))
    body = {"model": settings.model_image, "prompt": prompt, "images": [{"image_url": data_url(image)}], "n": 1,
            "response_format": "b64_json", "output_format": "png"}
    try:
        raw = _post_json("images/edits", body)
    except MuseError:
        # Multipart form via the SDK (documented alternative).
        resp = _client().images.edit(model=settings.model_image, image=("product.png", image, sniff_mime(image)), prompt=prompt)
        raw = resp.model_dump()
    return _to_png(_image_from_response(raw))


def mock_png(w: int, h: int, bg: tuple[int, int, int], fg: tuple[int, int, int]) -> bytes:
    """Tiny valid PNG (a filled circle on a background) without Pillow."""
    cx, cy, r2 = w / 2, h / 2, (min(w, h) * 0.38) ** 2
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            rows.extend(fg if (x - cx) ** 2 + (y - cy) ** 2 <= r2 else bg)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")

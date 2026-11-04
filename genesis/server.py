"""FastAPI app: WhatsApp + Mercado Pago webhooks, stage demo API, SSE for the panel, media, panel.

Run: ``uv run uvicorn genesis.server:app --host 0.0.0.0 --port 8000``

Handlers are sync (FastAPI runs them in a threadpool). Raw bodies are read by a tiny async
dependency so the webhook can verify ``X-Hub-Signature-256`` and still answer 200 immediately;
the real work happens in a background thread.
"""

from __future__ import annotations

import hmac
import html
import importlib
import io
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import quote
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import onboarding
from .config import settings
from .events import Event, bus

log = logging.getLogger("genesis.server")

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "panel" / "index.html"
FIXTURES = ROOT / "fixtures"
MEDIA_DIR = Path(os.getenv("GENESIS_MEDIA_DIR", str(ROOT / "media")))  # same default as lanes/_common.media_root
DEMO_OWNER_WA_ID = settings.demo_owner_wa_id  # Rosa's phone for the stage demo ("Tu negocio está vivo")
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_PHOTO_BYTES = 8 * 1024 * 1024
KEEPALIVE_S = 15.0
BACKGROUND = True  # tests set False to process webhooks inline
TERMINAL_KINDS = ("run.live", "run.failed")

app = FastAPI(title="Génesis", version="0.1.0")


# ------------------------------------------------------------------------------------- admin auth


def _admin_token() -> str:
    return os.getenv("GENESIS_ADMIN_TOKEN", "")


def require_admin(request: Request) -> None:
    """Stage API guard. The server is public (WhatsApp/Mercado Pago webhooks, catalog images), so
    building, confirming and reading runs needs GENESIS_ADMIN_TOKEN (header X-Genesis-Token or
    ?token=). Without a token it is open only in dry-run/mock; live it fails closed."""
    want = _admin_token()
    if not want:
        if settings.wa_live:
            raise HTTPException(503, "configura GENESIS_ADMIN_TOKEN para usar el panel con WhatsApp real")
        return
    got = request.headers.get("x-genesis-token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(got.encode(), want.encode()):
        raise HTTPException(401, "token de admin inválido")


ADMIN = [Depends(require_admin)]


def _startup_checks() -> None:
    if settings.wa_live and not settings.app_secret:
        log.error("WA_APP_SECRET vacío con WhatsApp real: /webhook rechazará todo (firma obligatoria)")
    if settings.wa_live and not _admin_token():
        log.error("GENESIS_ADMIN_TOKEN vacío con WhatsApp real: la API del panel queda cerrada")
    if settings.wa_live and not settings.intake_phone_id:
        log.error("WA_INTAKE_PHONE_ID vacío: no se puede avisar a la dueña")


_startup_checks()


def _mod(name: str) -> Any:
    return importlib.import_module(f"genesis.{name}")


def _bg(fn: Any, *args: Any) -> None:
    if not BACKGROUND:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("inline task failed")
        return

    def runner() -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("background task failed")

    threading.Thread(target=runner, daemon=True).start()


async def raw_body(request: Request) -> bytes:
    return await request.body()


async def form_data(request: Request) -> Any:
    return await request.form(max_files=8, max_fields=50)


# ------------------------------------------------------------------------------------- WhatsApp hook


@app.get("/webhook")
def verify_webhook(request: Request) -> PlainTextResponse:
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "bad verify token")


@app.post("/webhook")
def webhook(request: Request, body: bytes = Depends(raw_body)) -> dict:
    wa = _mod("whatsapp")
    if not wa.verify_signature(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(403, "bad signature")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return {"ok": False}
    _bg(_process_webhook, payload)
    return {"ok": True}


def _process_webhook(payload: dict) -> None:
    for inbound in _mod("whatsapp").parse_webhook(payload):
        _bg(route_inbound, inbound)


def _is_cradle(phone_id: str) -> bool:
    if phone_id in settings.cradle_phone_ids or phone_id == onboarding.MOCK_CRADLE:
        return True
    try:
        return _mod("store").get_business(phone_id) is not None
    except Exception:  # noqa: BLE001
        return False


def route_inbound(inbound: Any) -> str:
    """intake number -> onboarding; cradle number -> that business's agent. Returns the route taken."""
    intake = settings.intake_phone_id
    if intake and inbound.phone_id == intake:
        onboarding.handle_intake(inbound)
        return "intake"
    if _is_cradle(inbound.phone_id):
        if onboarding._first_time("panel:" + (inbound.message_id or "")):  # Meta redelivers webhooks
            _customer_event(inbound)
        _mod("agent").handle(inbound.phone_id, inbound)
        return "agent"
    if not intake:  # not configured: everything that is not a cradle is the intake number
        onboarding.handle_intake(inbound)
        return "intake"
    log.warning("message for unknown phone_id %s ignored", inbound.phone_id)
    return "ignored"


def _business_run(cradle: str) -> str:
    try:
        biz = _mod("store").get_business(cradle) or {}
    except Exception:  # noqa: BLE001
        return ""
    return biz.get("run_id") or ""


def _customer_event(inbound: Any) -> None:
    """Let the stage panel show the first customers writing to the brand-new business."""
    run_id = _business_run(inbound.phone_id)
    if not run_id:
        return
    if getattr(inbound, "flow_response", None) is not None:
        text = "envió el formulario de pedido"  # nfm_reply body is just "Sent"
    else:
        text = inbound.text or {"audio": "nota de voz", "image": "foto", "interactive": "pedido"}.get(inbound.type, inbound.type)
    who = inbound.name or onboarding._mask(inbound.wa_id)
    bus.emit(Event(run_id, "customer.msg", "customers", "", text, {"who": who, "type": inbound.type}))


# ---------------------------------------------------------------------------------------- payments


@app.post("/mp/webhook")
def mp_webhook(request: Request, body: bytes = Depends(raw_body)) -> dict:
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        payload = {}
    query = dict(request.query_params)
    _bg(_process_payment, payload, query)
    return {"ok": True}  # always 200 so Mercado Pago does not retry forever


def _process_payment(payload: dict, query: dict) -> None:
    order = _mod("payments").handle_webhook(payload, query)
    if order:
        _order_paid(order)


def _order_paid(order: dict) -> None:
    cradle = order.get("cradle_phone_id") or order.get("cradle") or ""
    run_id = _business_run(cradle) if cradle else ""
    if run_id:
        total = order.get("total_cop")
        msg = f"Pedido {order.get('id', '')} pagado" + (f" · ${int(total):,}".replace(",", ".") if total else "")
        bus.emit(Event(run_id, "order.paid", "customers", "", msg, {"order": order}))


def _mock_pay_allowed() -> bool:
    return settings.mock or not settings.mp_access_token


_PAY_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pago de prueba</title>
<style>body{{margin:0;font-family:system-ui,sans-serif;background:#0b0d12;color:#f3f4f6;display:grid;place-items:center;min-height:100vh}}
.c{{max-width:360px;margin:16px;padding:28px;border-radius:20px;background:#151924;text-align:center}}
h1{{font-size:22px;margin:0 0 8px}}p{{color:#a3a9b8}}button{{font:inherit;font-weight:600;border:0;border-radius:12px;
padding:14px 22px;background:#25d366;color:#05210f;cursor:pointer;width:100%}}.ok{{color:#25d366;font-size:44px}}</style></head>
<body><div class="c">{body}</div></body></html>"""


@app.get("/pay/mock/{order_id}", response_class=HTMLResponse)
def pay_mock_page(order_id: str, pay: int = 0) -> HTMLResponse:
    if not _mock_pay_allowed():
        raise HTTPException(404)
    oid = html.escape(order_id)
    if pay:
        return pay_mock(order_id)
    body = (f"<h1>Pago de prueba</h1><p>Pedido {oid}. Esto es una simulación: no se cobra nada.</p>"
            f'<form method="post" action="/pay/mock/{quote(order_id, safe="")}"><button>Pagar (simulado)</button></form>')
    return HTMLResponse(_PAY_PAGE.format(body=body))


@app.post("/pay/mock/{order_id}", response_class=HTMLResponse)
def pay_mock(order_id: str) -> HTMLResponse:
    if not _mock_pay_allowed():
        raise HTTPException(404)
    try:
        order = _mod("payments").mock_pay(order_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("mock pay failed")
        raise HTTPException(404, "pedido no encontrado") from exc
    if order:
        _order_paid(order)
    body = (f'<div class="ok">✓</div><h1>Pago aprobado</h1><p>Pedido {html.escape(order_id)}. '
            "Ya puedes volver a WhatsApp.</p>")
    return HTMLResponse(_PAY_PAGE.format(body=body))


@app.get("/pay/done", response_class=HTMLResponse)
def pay_done() -> HTMLResponse:
    """Mercado Pago back_url when the cradle number has no display number configured."""
    body = '<div class="ok">✓</div><h1>¡Gracias!</h1><p>Ya puedes volver a WhatsApp.</p>'
    return HTMLResponse(_PAY_PAGE.format(body=body))


# ---------------------------------------------------------------------------------------- demo API


def _demo_inputs() -> tuple[bytes, str, list[bytes]]:
    """fixtures/rosa_* (real voice note and photos if present; tiny generated ones otherwise)."""
    audio, mime = b"", "audio/ogg"
    mimes = {".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}
    for p in sorted(FIXTURES.glob("rosa_*")):
        if p.suffix.lower() in mimes:
            audio, mime = p.read_bytes(), mimes[p.suffix.lower()]
            break
    photos = [p.read_bytes() for p in sorted(FIXTURES.glob("rosa_*")) if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")][:3]
    if not audio:
        audio = b"OggS-mock-voice-note"
    if not photos:
        photos = [_placeholder_photo()]
    return audio, mime, photos


def _placeholder_photo() -> bytes:
    try:
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (512, 512), (245, 235, 215))
        d = ImageDraw.Draw(im)
        d.ellipse((96, 136, 416, 376), fill=(236, 190, 90), outline=(170, 110, 40), width=8)
        d.ellipse((190, 200, 320, 300), fill=(255, 250, 235))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return b"\xff\xd8\xff\xe0mock-photo"


def _read_upload(up: Any, limit: int) -> tuple[bytes, str]:
    data = up.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{up.filename}: archivo demasiado grande")
    return data, (up.content_type or "")


@app.post("/api/runs", dependencies=ADMIN)
def create_run(request: Request, form: Any = Depends(form_data)) -> JSONResponse:
    q = request.query_params
    demo = q.get("demo", form.get("demo", "0")) in ("1", "true", "yes")
    auto_confirm = q.get("auto_confirm", form.get("auto_confirm", "1")) in ("1", "true", "yes")
    owner = str(form.get("owner", "") or q.get("owner", "") or DEMO_OWNER_WA_ID)
    answer = str(form.get("answer", "") or "")
    audio_up = form.get("audio")
    uploads = [u for u in form.getlist("photos") + form.getlist("photos[]") if hasattr(u, "file")]
    if demo and not hasattr(audio_up, "file"):
        audio, mime, photos = _demo_inputs()
    else:
        if not hasattr(audio_up, "file"):
            raise HTTPException(422, "falta el audio (campo 'audio')")
        audio, mime = _read_upload(audio_up, MAX_AUDIO_BYTES)
        if not audio:
            raise HTTPException(422, "audio vacío")
        photos = []
        for up in uploads[: onboarding.MAX_PHOTOS]:
            data, ctype = _read_upload(up, MAX_PHOTO_BYTES)
            if data and (ctype.startswith("image/") or not ctype):
                photos.append(data)
        if demo and not photos:
            photos = _demo_inputs()[2]
        mime = mime or "audio/ogg"
    run_id = onboarding.start_run(owner, audio, mime, photos, answer, auto_confirm=auto_confirm)
    return JSONResponse({"run_id": run_id, "events": f"/api/runs/{run_id}/events", "auto_confirm": auto_confirm}, 201)


@app.get("/api/runs", dependencies=ADMIN)
def list_runs(limit: int = 20) -> list[dict]:
    return onboarding.list_runs(limit)


@app.get("/api/runs/{run_id}", dependencies=ADMIN)
def get_run(run_id: str) -> dict:
    run = onboarding.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    run["events"] = len(bus.log(run_id))
    return run


@app.post("/api/runs/{run_id}/confirm", dependencies=ADMIN)
def confirm_run(run_id: str) -> dict:
    """Stage helper: confirm a run waiting at the read-back (same as the seller saying "sí")."""
    if not onboarding.confirm(run_id):
        raise HTTPException(409, "run is not waiting for confirmation")
    return {"ok": True}


def _plain(ev: Event) -> str:
    return "data: " + ev.sse().split("\ndata: ", 1)[1]


def _sse(run_id: str, close_on_end: bool, plain: bool = False) -> StreamingResponse:
    q = bus.subscribe(run_id)

    def gen() -> Any:
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    ev = q.get(timeout=KEEPALIVE_S)
                except queue.Empty:
                    yield f": ping {int(time.time())}\n\n"
                    continue
                yield _plain(ev) if plain else ev.sse()
                if close_on_end and ev.kind in TERMINAL_KINDS:
                    break
        finally:
            bus.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/events", dependencies=ADMIN)
def run_events(run_id: str, close: int = 0, plain: int = 0) -> StreamingResponse:
    """SSE with backlog. ``?close=1`` ends the stream after run.live / run.failed; ``?plain=1`` sends
    unnamed messages (the event kind is inside the JSON) so a browser gets every kind via onmessage."""
    return _sse(run_id, bool(close), bool(plain))


@app.get("/api/stream", dependencies=ADMIN)
def all_events(plain: int = 0) -> StreamingResponse:
    """Live events of every run (no backlog): the panel uses it to follow runs started on WhatsApp."""
    return _sse("*", False, bool(plain))


@app.get("/api/businesses", dependencies=ADMIN)
def businesses() -> list[dict]:
    store = _mod("store")
    if hasattr(store, "list_businesses"):
        rows = store.list_businesses()
    else:
        ids = list(dict.fromkeys(settings.cradle_phone_ids + [onboarding.MOCK_CRADLE]))
        rows = [dict(b, cradle_phone_id=c) for c in ids if (b := store.get_business(c))]
    out = []
    for b in rows:
        spec = b.get("spec")
        cradle = b.get("cradle_phone_id", "")
        try:
            orders = len(store.list_orders(cradle)) if cradle else 0
        except Exception:  # noqa: BLE001
            orders = 0
        out.append({
            "cradle_phone_id": cradle,
            "business_name": getattr(spec, "business_name", "") if spec is not None else "",
            "spec": spec.model_dump(mode="json") if hasattr(spec, "model_dump") else spec,
            "live": bool(b.get("live")),
            "run_id": b.get("run_id", ""),
            "flow_id": b.get("flow_id", ""),
            "orders": orders,
        })
    return out


# ------------------------------------------------------------------------------------ media, panel


@app.get("/media/{path:path}")
def media(path: str) -> FileResponse:
    base = MEDIA_DIR.resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "mock": settings.mock, "wa_live": settings.wa_live}


@app.get("/")
def panel() -> FileResponse:
    return FileResponse(PANEL, media_type="text/html")

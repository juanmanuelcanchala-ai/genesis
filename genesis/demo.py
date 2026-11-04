"""Offline end-to-end rehearsal of the stage demo (mock models + WhatsApp dry-run, no network).

Everything enters through the real HTTP surface of ``genesis.server`` (FastAPI TestClient) with
real WhatsApp webhook payloads:

1. Rosa sends the intake number a voice note + 2 photos and writes "listo".
2. Génesis transcribes, compiles the spec and asks ONE clarifying question (in this scenario Rosa
   forgot to say whether she delivers).
3. Rosa answers, gets the summary with buttons, says "sí".
4. The plan runs every lane, the inspector passes, the business goes live and Rosa gets the link.
5. A customer writes to the cradle number, asks a price, orders through the Flow (nfm_reply),
   gets the payment link, pays on the mock checkout page, and Rosa is notified.

Used by ``scripts/demo_offline.py`` (prints the timeline) and ``tests/test_e2e.py`` (asserts).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .config import settings
from .events import Event, bus

ROSA_WA_ID = settings.demo_owner_wa_id or "573101234567"
ROSA_NAME = "Rosa"
CUSTOMER_WA_ID = "573209876543"
CUSTOMER_NAME = "Andrés"
WABA_ID = settings.waba_id or "100000000000001"
INTAKE_DISPLAY = "573000000000"

ANSWER = "Solo para recoger aquí en el puesto, no hago domicilios."

# Rosa's voice note for this scenario: the fixture minus the sentence about pickup/delivery, so the
# compiler has to ask exactly one question.
_DROPPED_SEGMENT = "recoge"


@dataclass
class Line:
    t: float
    who: str
    text: str
    kind: str = ""


@dataclass
class Result:
    run_id: str = ""
    cradle: str = ""
    wa_me: str = ""
    question: str = ""
    order: dict[str, Any] = field(default_factory=dict)
    paid: dict[str, Any] = field(default_factory=dict)
    lines: list[Line] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    t0: float = 0.0
    elapsed_ms: int = 0

    def timeline(self) -> str:
        out = []
        for ln in sorted(self.lines, key=lambda x: x.t):
            ms = int((ln.t - self.t0) * 1000)
            text = ln.text.replace("\n", " / ")
            if len(text) > 150:
                text = text[:147] + "..."
            out.append(f"+{ms:6d} ms  {ln.who:<22} {text}")
        return "\n".join(out)

    def sent_to(self, wa_id: str, op: str | None = None) -> list[dict[str, Any]]:
        return [s for s in self.sent if s["payload"].get("to") == wa_id and (op is None or s["op"] == op)]


# ------------------------------------------------------------------------------ webhook payloads


def _wamid() -> str:
    return "wamid.HBgM" + uuid.uuid4().hex.upper()[:24]


def wa_payload(phone_number_id: str, display: str, wa_id: str, name: str, message: dict[str, Any]) -> dict:
    """A WhatsApp Cloud API `messages` webhook exactly as Meta posts it."""
    message = {"from": wa_id, "id": _wamid(), "timestamp": str(int(time.time())), **message}
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": WABA_ID,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": display, "phone_number_id": phone_number_id},
                    "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                    "messages": [message],
                },
            }],
        }],
    }


def msg_text(body: str) -> dict:
    return {"type": "text", "text": {"body": body}}


def msg_audio(media_id: str) -> dict:
    return {"type": "audio", "audio": {"mime_type": "audio/ogg; codecs=opus", "sha256": "x", "id": media_id, "voice": True}}


def msg_image(media_id: str, caption: str = "") -> dict:
    img: dict[str, Any] = {"mime_type": "image/jpeg", "sha256": "x", "id": media_id}
    if caption:
        img["caption"] = caption
    return {"type": "image", "image": img}


def msg_button_reply(button_id: str, title: str) -> dict:
    return {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}}}


def msg_nfm_reply(response: dict[str, Any]) -> dict:
    return {
        "type": "interactive",
        "context": {"from": INTAKE_DISPLAY, "id": _wamid()},
        "interactive": {
            "type": "nfm_reply",
            "nfm_reply": {"response_json": json.dumps(response, ensure_ascii=False), "body": "Sent", "name": "flow"},
        },
    }


# ------------------------------------------------------------------------------------- scenario


def _scenario_transcript(muse: Any) -> Any:
    fx = muse.load_fixture("rosa_transcript.json")
    segs = [s for s in fx["segments"] if _DROPPED_SEGMENT not in s["text"]]
    return muse.Transcript(text=" ".join(s["text"] for s in segs), segments=segs, model=fx.get("model", "mock"))


def _scenario_spec(muse: Any) -> Any:
    """Mock structured output: without the answer the model cannot know the fulfillment."""

    def answer(prompt: str, schema: dict, images: Any) -> dict:
        spec = muse.load_fixture("rosa_spec.json")
        if "El vendedor respondió" in prompt:
            return spec
        spec["fulfillment"] = None
        spec["missing"] = ["fulfillment"]
        spec["clarifying_question"] = ""
        (spec.get("evidence") or {}).pop("fulfillment", None)
        return spec

    return answer


@contextlib.contextmanager
def _sandbox(workdir: Path) -> Iterator[None]:
    """Point every module at a throwaway DB/media dir and run the pipeline inline."""
    from . import muse, onboarding, server, store, whatsapp

    saved_mod = [
        (onboarding, "BACKGROUND", onboarding.BACKGROUND),
        (onboarding, "DB_PATH", onboarding.DB_PATH),
        (onboarding, "AUTO_COMPILE_S", onboarding.AUTO_COMPILE_S),
        (server, "BACKGROUND", server.BACKGROUND),
        (server, "MEDIA_DIR", server.MEDIA_DIR),
        (muse, "transcribe", muse.transcribe),
    ]
    saved_env = os.environ.get("GENESIS_MEDIA_DIR")
    saved_structured = muse.MOCK_STRUCTURED.get("business_spec")
    media = workdir / "media"
    media.mkdir(parents=True, exist_ok=True)
    os.environ["GENESIS_MEDIA_DIR"] = str(media)
    onboarding.BACKGROUND = False
    onboarding.DB_PATH = str(workdir / "intake.db")
    onboarding.AUTO_COMPILE_S = 0
    server.BACKGROUND = False
    server.MEDIA_DIR = media
    scenario = _scenario_transcript(muse)
    muse.transcribe = lambda audio, mime="audio/ogg": scenario  # type: ignore[assignment]
    muse.MOCK_STRUCTURED["business_spec"] = _scenario_spec(muse)
    store.reset(str(workdir / "genesis.db"))
    onboarding.reset()
    whatsapp.reset()
    try:
        yield
    finally:
        for mod, name, val in saved_mod:
            setattr(mod, name, val)
        if saved_structured is None:
            muse.MOCK_STRUCTURED.pop("business_spec", None)
        else:
            muse.MOCK_STRUCTURED["business_spec"] = saved_structured
        if saved_env is None:
            os.environ.pop("GENESIS_MEDIA_DIR", None)
        else:
            os.environ["GENESIS_MEDIA_DIR"] = saved_env
        store.reset(None)
        onboarding.reset()


_SHOW_EVENTS = {
    "run.started", "intake.compiling", "spec.ready", "intake.asking", "intake.confirming", "intake.building",
    "plan.ready", "node.done", "node.failed", "node.skipped", "node.reverted", "inspect.check", "inspect.done",
    "run.live", "run.failed", "customer.msg", "order.paid",
}
_SHOW_OPS = {"text", "buttons", "flow", "cta_url", "image", "template"}


def _event_line(ev: Event) -> Line | None:
    if ev.kind not in _SHOW_EVENTS:
        return None
    d = ev.data or {}
    if ev.kind == "spec.ready":
        text = f"spec: {ev.message} · {len(d.get('highlights') or [])} campos con su segundo de audio"
        if d.get("question"):
            text += " · falta 1 dato"
    elif ev.kind == "node.done":
        text = f"✓ {ev.node} ({d.get('ms', 0)} ms)" + (" [caché]" if d.get("cached") else "")
    elif ev.kind == "inspect.check":
        ok = "✓" if d.get("ok") else "✗"
        text = f"{ok} inspector: {d.get('name') or ev.message}"
    elif ev.kind == "plan.ready":
        text = f"plan: {ev.message} en {len({n['lane'] for n in d.get('nodes', [])})} carriles"
    else:
        text = f"{ev.kind}: {ev.message}"
    return Line(ev.ts, "panel", text, ev.kind)


def _sent_text(op: str, p: dict[str, Any]) -> str:
    if op == "text":
        return p["text"]["body"]
    it = p.get("interactive") or {}
    body = (it.get("body") or {}).get("text", "")
    if op == "buttons":
        btns = " | ".join(b["reply"]["title"] for b in it["action"]["buttons"])
        return f"{body} [{btns}]"
    if op == "flow":
        return f"{body} [Flow: {it['action']['parameters']['flow_cta']}]"
    if op == "cta_url":
        prm = it["action"]["parameters"]
        return f"{body} [{prm['display_text']} → {prm['url']}]"
    return op


def run(workdir: str | Path | None = None, *, echo: Any = None) -> Result:
    """Run the whole demo offline. ``echo(line)`` is called as lines happen (optional)."""
    if not settings.mock or settings.wa_live:
        raise RuntimeError("the offline demo needs GENESIS_MOCK=1 and WhatsApp in dry-run (WA_DRY_RUN=1)")
    from fastapi.testclient import TestClient

    from . import flowgen, onboarding, server, store, whatsapp

    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="genesis-demo-")
        workdir = tmp.name
    res = Result()
    res.t0 = time.time()
    wd = Path(workdir)

    def add(who: str, text: str, kind: str = "") -> None:
        ln = Line(time.time(), who, text, kind)
        res.lines.append(ln)
        if echo:
            echo(ln, res.t0)

    orig_record = whatsapp._record
    q = bus.subscribe("*")

    def record(op: str, payload: dict[str, Any], prefix: str = "dry") -> str:
        mid = orig_record(op, payload, prefix)
        res.sent.append({"op": op, "payload": payload, "id": mid, "t": time.time()})
        if op in _SHOW_OPS:
            to = payload.get("to", "")
            who = {ROSA_WA_ID: "Génesis → Rosa", CUSTOMER_WA_ID: "Negocio → cliente"}.get(to, f"→ {to}")
            add(who, _sent_text(op, payload), "wa." + op)
        return mid

    try:
        with _sandbox(wd):
            whatsapp._record = record  # type: ignore[assignment]
            client = TestClient(server.app)
            intake = settings.intake_phone_id or "intake"

            def post(phone_id: str, display: str, wa_id: str, name: str, message: dict, label: str) -> None:
                add(f"{name} → {'Génesis' if phone_id == intake else 'negocio'}", label, "wa.in")
                body = json.dumps(wa_payload(phone_id, display, wa_id, name, message), ensure_ascii=False).encode()
                headers = {"Content-Type": "application/json"}
                if settings.app_secret:
                    sig = hmac.new(settings.app_secret.encode(), body, hashlib.sha256).hexdigest()
                    headers["X-Hub-Signature-256"] = f"sha256={sig}"
                r = client.post("/webhook", content=body, headers=headers)
                if r.status_code != 200:
                    raise RuntimeError(f"webhook {r.status_code}: {r.text}")

            # --- 1. Rosa on the intake number: voice note + photos + "listo"
            audio, mime, photos = server._demo_inputs()
            if len(photos) < 2:
                photos = photos + [server._placeholder_photo()]
            whatsapp.DRY_MEDIA["rosa-voice-1"] = (audio, mime)
            for i, ph in enumerate(photos[:2]):
                whatsapp.DRY_MEDIA[f"rosa-photo-{i + 1}"] = (ph, "image/jpeg")
            post(intake, INTAKE_DISPLAY, ROSA_WA_ID, ROSA_NAME, msg_audio("rosa-voice-1"), f"🎤 nota de voz ({len(audio)} bytes)")
            for i in range(min(2, len(photos))):
                post(intake, INTAKE_DISPLAY, ROSA_WA_ID, ROSA_NAME, msg_image(f"rosa-photo-{i + 1}"), f"📷 foto {i + 1}")
            post(intake, INTAKE_DISPLAY, ROSA_WA_ID, ROSA_NAME, msg_text("listo"), "listo")

            run_id = onboarding.SESSIONS[ROSA_WA_ID].run_id
            res.run_id = run_id
            info = onboarding.get_run(run_id) or {}
            res.question = info.get("question", "")
            if info.get("state") != "asking":
                raise RuntimeError(f"expected a clarifying question, run is {info.get('state')}")

            # --- 2. answer, read-back, "sí"
            post(intake, INTAKE_DISPLAY, ROSA_WA_ID, ROSA_NAME, msg_text(ANSWER), ANSWER)
            if (onboarding.get_run(run_id) or {}).get("state") != "confirming":
                raise RuntimeError("expected the summary read-back after the answer")
            post(intake, INTAKE_DISPLAY, ROSA_WA_ID, ROSA_NAME, msg_text("Sí"), "Sí")

            info = onboarding.get_run(run_id) or {}
            if info.get("state") != "live":
                raise RuntimeError(f"run did not go live: {info.get('state')} {info.get('error')}")
            res.cradle, res.wa_me = info["cradle_phone_id"], info["wa_me"]
            display = info.get("display_number") or onboarding.MOCK_DISPLAY

            # --- 3. a customer writes to the brand-new business
            cradle = res.cradle
            for text in ("Hola", "¿A cómo la arepa de choclo con queso?", "Quiero hacer un pedido"):
                post(cradle, display, CUSTOMER_WA_ID, CUSTOMER_NAME, msg_text(text), text)
            biz = store.get_business(cradle)
            assert biz is not None
            spec = biz["spec"]
            ids = [p.id for p in spec.products]
            qty = {ids[0]: 2} | ({ids[1]: 1} if len(ids) > 1 else {})
            slots = flowgen.pickup_slots(spec)
            pickup = "07:30" if "07:30" in slots else slots[min(3, len(slots) - 1)]
            response = flowgen.example_response(spec, qty, customer_name=CUSTOMER_NAME, pickup_time=pickup,
                                                flow_token=f"order:{cradle}:{CUSTOMER_WA_ID}")
            post(cradle, display, CUSTOMER_WA_ID, CUSTOMER_NAME, msg_nfm_reply(response),
                 "📝 envía el formulario: " + ", ".join(f"{v} x {k}" for k, v in qty.items()))

            pay = [s for s in res.sent if s["op"] == "cta_url" and s["payload"]["to"] == CUSTOMER_WA_ID]
            if not pay:
                raise RuntimeError("customer did not get a payment link")
            url = pay[-1]["payload"]["interactive"]["action"]["parameters"]["url"]
            path = urlparse(url).path
            orders = [o for o in store.list_orders(cradle) if not o.get("test")]
            res.order = orders[0] if orders else {}

            # --- 4. customer pays on the (mock) checkout page
            add(f"{CUSTOMER_NAME} (navegador)", f"abre el link de pago {path}", "pay.open")
            page = client.get(path)
            if page.status_code != 200:
                raise RuntimeError(f"checkout page {page.status_code}")
            action = page.text.split('action="', 1)[1].split('"', 1)[0]
            add(f"{CUSTOMER_NAME} (navegador)", "toca «Pagar (simulado)»", "pay.submit")
            done = client.post(action)
            if done.status_code != 200 or "Pago aprobado" not in done.text:
                raise RuntimeError(f"mock payment failed: {done.status_code}")
            res.paid = store.get_order(res.order["id"]) or {}
    finally:
        whatsapp._record = orig_record  # type: ignore[assignment]
        bus.unsubscribe("*", q)
        while not q.empty():
            ev = q.get_nowait()
            res.events.append(ev)
            ln = _event_line(ev)
            if ln:
                res.lines.append(ln)
        res.elapsed_ms = int((time.time() - res.t0) * 1000)
        if tmp is not None:
            tmp.cleanup()
    return res

"""Intake conversation on the Génesis number: voice note + photos -> spec -> confirm -> build.

State per seller (wa_id): ``collecting`` (one voice note, up to 3 photos; "listo" or 20 s after the
last item, once there is audio and at least one photo) -> ``compiling`` -> ``asking`` (at most ONE
clarifying question) -> ``confirming`` (summary read-back, wait for "sí") -> ``building`` (thread
running ``plan.apply_plan`` on a free cradle number) -> ``live`` (or ``failed`` / ``blocked``).

State lives in memory (``RUNS``, ``SESSIONS``) and is mirrored to a small SQLite table ``intake``
so a run can still be looked up after a restart. Sibling modules are imported lazily so this file
works (and is testable) while they are being written.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import settings
from .events import Event, bus
from .spec import BLOCKED_CATEGORIES, BusinessSpec

log = logging.getLogger("genesis.onboarding")

AUTO_COMPILE_S = float(os.getenv("GENESIS_INTAKE_WAIT_S", "20"))
MAX_PHOTOS = 3
MOCK_CRADLE = "cradle-1"
MOCK_DISPLAY = "573000000001"
BACKGROUND = True  # tests set False to run the pipeline inline
DB_PATH: str | None = None  # None -> settings.db_path (tests point it to a tmp file)

BTN_YES = "confirm_yes"
BTN_EDIT = "confirm_edit"
TERMINAL = ("live", "failed", "blocked")

DAYS_ES = {"mon": "lun", "tue": "mar", "wed": "mié", "thu": "jue", "fri": "vie", "sat": "sáb", "sun": "dom"}
FULFILLMENT_ES = {"pickup": "Para recoger", "delivery": "Domicilio", "both": "Recoger o domicilio"}
PAYMENT_ES = {"cash": "Efectivo", "link": "Link de pago", "transfer": "Transferencia"}


def _mod(name: str) -> Any:
    return importlib.import_module(f"genesis.{name}")


# --------------------------------------------------------------------------------------------- state


@dataclass
class Run:
    run_id: str
    owner_wa_id: str
    audio: bytes
    mime: str
    photos: list[bytes]
    answer: str = ""
    auto_confirm: bool = True
    via: str = "api"  # api | whatsapp
    state: str = "compiling"
    draft: Any = None
    question: str = ""
    question_asked: bool = False
    cradle_phone_id: str = ""
    display_number: str = ""
    wa_me: str = ""
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    outputs: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> BusinessSpec | None:
        return getattr(self.draft, "spec", None)

    def public(self) -> dict[str, Any]:
        spec = self.spec
        end = self.finished or time.time()
        return {
            "run_id": self.run_id,
            "state": self.state,
            "via": self.via,
            "owner": _mask(self.owner_wa_id),
            "auto_confirm": self.auto_confirm,
            "photos": len(self.photos),
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round(end - self.started, 1),
            "business_name": spec.business_name if spec else "",
            "spec": spec.model_dump(mode="json") if spec else None,
            "question": self.question,
            "cradle_phone_id": self.cradle_phone_id,
            "display_number": self.display_number,
            "wa_me": self.wa_me,
            "error": self.error,
        }


@dataclass
class Session:
    wa_id: str
    name: str = ""
    audio: bytes | None = None
    mime: str = "audio/ogg"
    photos: list[bytes] = field(default_factory=list)
    run_id: str = ""
    timer: threading.Timer | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


RUNS: dict[str, Run] = {}
SESSIONS: dict[str, Session] = {}
_lock = threading.Lock()


def _session(wa_id: str) -> Session:
    with _lock:
        s = SESSIONS.get(wa_id)
        if s is None:
            s = SESSIONS[wa_id] = Session(wa_id)
        return s


def get_run(run_id: str) -> dict[str, Any] | None:
    run = RUNS.get(run_id)
    if run is not None:
        return run.public()
    return _db_get(run_id)


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    runs = sorted(RUNS.values(), key=lambda r: r.started, reverse=True)[:limit]
    return [r.public() for r in runs]


def reset() -> None:
    """Forget in-memory state (tests and stage rehearsals)."""
    with _lock:
        for s in SESSIONS.values():
            if s.timer:
                s.timer.cancel()
        SESSIONS.clear()
        RUNS.clear()


# ------------------------------------------------------------------------------------------- SQLite

_db_conn: sqlite3.Connection | None = None
_db_conn_path: str | None = None
_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    global _db_conn, _db_conn_path
    path = DB_PATH or settings.db_path
    if _db_conn is None or _db_conn_path != path:
        _db_conn = sqlite3.connect(path, check_same_thread=False)
        _db_conn_path = path
        _db_conn.execute(
            "create table if not exists intake(run_id text primary key, wa_id text, via text, state text,"
            " spec text, question text, cradle text, wa_me text, error text, started real, updated real)"
        )
        _db_conn.commit()
    return _db_conn


def _db_save(run: Run) -> None:
    try:
        spec = run.spec.model_dump_json() if run.spec else None
        with _db_lock:
            db = _db()
            db.execute(
                "insert or replace into intake values(?,?,?,?,?,?,?,?,?,?,?)",
                (run.run_id, run.owner_wa_id, run.via, run.state, spec, run.question, run.cradle_phone_id,
                 run.wa_me, run.error, run.started, time.time()),
            )
            db.commit()
    except Exception:  # noqa: BLE001 - persistence must never break the stage demo
        log.exception("intake db save failed")


def _db_get(run_id: str) -> dict[str, Any] | None:
    try:
        with _db_lock:
            row = _db().execute(
                "select run_id, wa_id, via, state, spec, question, cradle, wa_me, error, started, updated"
                " from intake where run_id=?",
                (run_id,),
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    spec = json.loads(row[4]) if row[4] else None
    return {
        "run_id": row[0], "owner": _mask(row[1]), "via": row[2], "state": row[3], "spec": spec,
        "business_name": (spec or {}).get("business_name", ""), "question": row[5], "cradle_phone_id": row[6],
        "wa_me": row[7], "error": row[8], "started": row[9], "finished": row[10] if row[3] in TERMINAL else None,
    }


# ------------------------------------------------------------------------------------------ helpers


def _mask(wa_id: str) -> str:
    return f"•••{wa_id[-4:]}" if len(wa_id) > 4 else wa_id


def _intake_phone() -> str:
    return settings.intake_phone_id or "intake"


def _emit(run: Run, kind: str, message: str = "", lane: str = "intake", node: str = "", **data: Any) -> None:
    bus.emit(Event(run.run_id, kind, lane, node, message, data))


def _say(run: Run | None, wa_id: str, text: str) -> None:
    """Send a WhatsApp text from the intake number. Only for runs that came through WhatsApp."""
    if run is not None and run.via != "whatsapp":
        return
    if run is not None:
        _emit(run, "msg", text, who="genesis")
    try:
        _mod("whatsapp").send_text(_intake_phone(), wa_id, text)
    except Exception:  # noqa: BLE001
        log.exception("send_text to seller failed")


def _ask_confirm(run: Run, body: str) -> None:
    _emit(run, "msg", body, who="genesis")
    if run.via != "whatsapp":
        return
    wa = _mod("whatsapp")
    try:
        if len(body) > 1000:  # interactive body limit is 1024
            wa.send_text(_intake_phone(), run.owner_wa_id, body)
            body = "¿Lo creo así?"
        wa.send_buttons(_intake_phone(), run.owner_wa_id, body, [(BTN_YES, "Sí, créalo"), (BTN_EDIT, "Corregir algo")])
    except Exception:  # noqa: BLE001
        log.exception("send_buttons to seller failed")


def _spawn(fn: Any, *args: Any) -> None:
    if not BACKGROUND:
        fn(*args)
        return

    def runner() -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("onboarding background task failed")

    threading.Thread(target=runner, daemon=True, name=f"onboarding-{getattr(fn, '__name__', 'task')}").start()


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()


_DONE = {"listo", "lista", "ya", "ya esta", "termine", "eso es todo", "eso es", "fin", "ya termine", "listo ya"}


def is_done(text: str) -> bool:
    """Seller says they finished sending the voice note and photos."""
    t = _norm(text)
    return t in _DONE or t.startswith("listo")


def _cop(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


def highlights(spec: BusinessSpec) -> list[dict[str, Any]]:
    """Fields the panel lights up next to the second of audio they were heard at."""
    ev = spec.evidence or {}
    out: list[dict[str, Any]] = []

    def add(fld: str, label: str, value: str, evidence: Any) -> None:
        if not value:
            return
        out.append({
            "field": fld,
            "label": label,
            "value": value,
            "source_s": getattr(evidence, "source_s", None),
            "quote": getattr(evidence, "quote", "") or "",
            "photo": getattr(evidence, "photo", None),
        })

    add("business_name", "Negocio", spec.business_name, ev.get("business_name"))
    add("owner_name", "Dueña", spec.owner_name, ev.get("owner_name"))
    add("category", "Categoría", spec.category, ev.get("category"))
    for p in spec.products:
        add(f"products.{p.id}", "Producto", f"{p.name} · {_cop(p.price_cop)}", p.evidence)
    if spec.hours:
        days = spec.hours.days
        d = f"{DAYS_ES.get(days[0], days[0])}–{DAYS_ES.get(days[-1], days[-1])}" if len(days) > 1 else ", ".join(DAYS_ES.get(x, x) for x in days)
        add("hours", "Horario", f"{d} {spec.hours.open}–{spec.hours.close}", spec.hours.evidence)
    place = ", ".join(x for x in (spec.neighborhood, spec.city) if x)
    add("neighborhood", "Dónde", place, ev.get("neighborhood") or ev.get("address_hint") or ev.get("city"))
    add("fulfillment", "Entrega", FULFILLMENT_ES.get(spec.fulfillment.value, spec.fulfillment.value), ev.get("fulfillment"))
    add("delivery_zone", "Zona", spec.delivery_zone, ev.get("delivery_zone"))
    pays = " + ".join(PAYMENT_ES.get(m.value, m.value) for m in spec.payment_methods)
    add("payment_methods", "Pagos", pays, ev.get("payment_methods"))
    out.sort(key=lambda h: (h["source_s"] is None, h["source_s"] or 0))
    return out


def _transcript_dict(draft: Any) -> dict[str, Any]:
    t = getattr(draft, "transcript", None)
    return {
        "text": getattr(t, "text", "") or "",
        "segments": list(getattr(t, "segments", []) or []),
        "model": getattr(t, "model", "") or "",
    }


def _emit_spec(run: Run) -> None:
    spec = run.spec
    if spec is None:
        return
    _emit(
        run,
        "spec.ready",
        spec.business_name,
        spec=spec.model_dump(mode="json"),
        transcript=_transcript_dict(run.draft),
        highlights=highlights(spec),
        question=run.question,
    )


# ------------------------------------------------------------------------------------------ pipeline


def start_run(owner_wa_id: str, audio: bytes, mime: str, photos: list[bytes], answer: str = "", *,
              auto_confirm: bool = True, via: str = "api") -> str:
    """Start a run (compile -> [ask] -> [confirm] -> build). Returns run_id immediately.

    ``auto_confirm`` (stage demo) skips the clarifying question and the read-back.
    """
    run = Run(
        run_id=time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:6],
        owner_wa_id=owner_wa_id,
        audio=audio,
        mime=mime or "audio/ogg",
        photos=list(photos)[:MAX_PHOTOS],
        answer=answer,
        auto_confirm=auto_confirm,
        via=via,
    )
    with _lock:
        RUNS[run.run_id] = run
    _emit(run, "run.started", "Nota de voz recibida", via=via, photos=len(run.photos), audio_bytes=len(audio),
          auto_confirm=auto_confirm, owner=_mask(owner_wa_id))
    _db_save(run)
    _spawn(_compile, run)
    return run.run_id


def _compile(run: Run) -> None:
    run.state = "compiling"
    _emit(run, "intake.compiling", "Transcribiendo y entendiendo el negocio")
    try:
        draft = _mod("compiler").compile_spec(run.audio, run.mime, run.photos, answer=run.answer)
    except Exception as exc:  # noqa: BLE001
        _fail(run, f"No pude entender la nota de voz: {exc}",
              "Uy, no pude entender bien la nota de voz. ¿Me la mandas otra vez, un poquito más despacio?")
        return
    run.draft = draft
    run.question = getattr(draft, "question", "") or ""
    blocked = getattr(draft, "blocked", None) or getattr(draft.spec, "blocked_category", None)
    if blocked:
        what = BLOCKED_CATEGORIES.get(blocked, blocked)
        run.state = "blocked"
        run.finished = time.time()
        _emit_spec(run)
        _emit(run, "run.failed", f"Categoría no permitida: {what}", blocked=blocked)
        _db_save(run)
        _say(run, run.owner_wa_id,
             f"Lo siento, por las políticas de WhatsApp no puedo crear negocios de {what}. "
             "Si vendes otra cosa, mándame una nota de voz nueva y lo armamos.")
        return
    _emit_spec(run)
    if run.question and not run.answer and not run.auto_confirm and not run.question_asked:
        run.state = "asking"
        run.question_asked = True
        _db_save(run)
        _emit(run, "intake.asking", run.question)
        _say(run, run.owner_wa_id, run.question)
        return
    _after_spec(run)


def _after_spec(run: Run) -> None:
    if run.auto_confirm:
        _build(run)
        return
    run.state = "confirming"
    _db_save(run)
    _emit(run, "intake.confirming", "Esperando el sí del vendedor")
    _ask_confirm(run, _mod("compiler").summary(run.spec))


def _refine(run: Run, text: str) -> None:
    """Merge the seller's answer (or correction) and read the summary back again."""
    run.state = "compiling"
    _emit(run, "msg", text, who="seller")
    try:
        draft = _mod("compiler").compile_spec(run.audio, run.mime, run.photos, answer=text, previous=run.draft)
    except Exception:  # noqa: BLE001
        log.exception("refine failed")
        run.state = "confirming"
        _say(run, run.owner_wa_id, "No te entendí bien. ¿Me lo repites en un mensaje corto?")
        return
    run.draft = draft
    run.answer = text
    run.question = ""  # only ONE question per run
    _emit_spec(run)
    _after_spec(run)


def confirm(run_id: str) -> bool:
    """Seller said yes: start building. Returns False if the run is not waiting for confirmation."""
    with _lock:  # the panel button and the seller's "sí" can arrive at the same time
        run = RUNS.get(run_id)
        if run is None or run.state != "confirming":
            return False
        run.state = "building"
    _say(run, run.owner_wa_id, "¡De una! Estoy armando tu negocio. Te aviso en unos minutos.")
    _spawn(_build, run)
    return True


class NoCradle(RuntimeError):
    pass


_cradle_lock = threading.Lock()  # assign_cradle + run.cradle_phone_id must be atomic across runs


def assign_cradle(owner_wa_id: str) -> tuple[str, str]:
    """First configured cradle number with no business, else one already owned by this seller."""
    ids = settings.cradle_phone_ids
    displays = settings.cradle_display_numbers
    busy = {r.cradle_phone_id for r in list(RUNS.values()) if r.state == "building" and r.cradle_phone_id}
    if not ids:
        if MOCK_CRADLE in busy:  # another run is building on the only (mock) number
            raise NoCradle("el número de prueba está ocupado con otra construcción")
        return MOCK_CRADLE, displays[0] if displays else MOCK_DISPLAY
    try:
        get_business = _mod("store").get_business
    except Exception:  # noqa: BLE001
        def get_business(_cid: str) -> dict | None:
            return None
    owned: list[int] = []
    for i, cid in enumerate(ids):
        if cid in busy:
            continue
        biz = get_business(cid)
        if biz is None:
            return cid, displays[i] if i < len(displays) else ""
        if biz.get("owner_wa_id") == owner_wa_id:
            owned.append(i)
    if owned:
        i = owned[0]
        return ids[i], displays[i] if i < len(displays) else ""
    raise NoCradle("no hay números cuna libres")


def _build(run: Run) -> None:
    run.state = "building"
    spec = run.spec
    if spec is None:
        _fail(run, "sin spec", "")
        return
    try:
        with _cradle_lock:
            cradle, display = assign_cradle(run.owner_wa_id)
            run.cradle_phone_id, run.display_number = cradle, display
    except NoCradle as exc:
        _fail(run, str(exc), "Ahora mismo no tengo números libres. Te escribo apenas se libere uno.")
        return
    _db_save(run)
    _emit(run, "intake.building", f"Construyendo en {cradle}", cradle=cradle, display_number=display)
    plan = _mod("plan")
    ctx = plan.Ctx(run_id=run.run_id, spec=spec, photos=list(run.photos), cradle_phone_id=cradle,
                   cradle_display_number=display, owner_wa_id=run.owner_wa_id)
    # Also under outputs for lanes/stubs that read it from there.
    intake = {"owner_wa_id": run.owner_wa_id, "transcript": _transcript_dict(run.draft)["text"], "via": run.via}
    ctx.put("intake", intake)
    ctx.put("run", intake)  # lanes/_common.owner_wa_id also looks here
    # In mock every rehearsal should show the whole graph, not "ya existía" cache hits. In WhatsApp dry-run
    # the outputs hold fake ids (flow.dry…), which must never be cached where a live run would reuse them.
    state = plan.State(":memory:") if settings.mock or not settings.wa_live else None
    try:
        nodes = _mod("blueprint").build(ctx)
        outputs = plan.apply_plan(ctx, nodes, state=state)
    except Exception as exc:  # noqa: BLE001 - apply_plan already reverted and emitted run.failed
        already = any(e.kind == "run.failed" for e in bus.log(run.run_id))
        _fail(run, str(exc), "Algo falló al armar tu negocio y deshice todo para no dejarlo a medias. "
              "Ya lo estamos revisando.", emit=not already)
        return
    run.outputs = outputs
    launch = outputs.get("launch.live", {}) if isinstance(outputs, dict) else {}
    run.wa_me = launch.get("wa_me") or launch.get("link") or _wa_me(display, spec)
    run.state = "live"
    run.finished = time.time()
    _db_save(run)
    inspector = outputs.get("inspector.run", {}) if isinstance(outputs, dict) else {}
    _emit(
        run,
        "run.live",
        "Tu negocio está vivo",
        lane="launch",
        wa_me=run.wa_me,
        display_number=display,
        cradle=cradle,
        business_name=spec.business_name,
        elapsed_s=round(run.finished - run.started, 1),
        inspector=inspector,
    )


def _wa_me(display: str, spec: BusinessSpec) -> str:
    text = f"Hola {spec.business_name}, quiero hacer un pedido"
    try:
        return _mod("whatsapp").wa_me_link(display, text)
    except Exception:  # noqa: BLE001
        from urllib.parse import quote

        return f"https://wa.me/{re.sub(r'[^0-9]', '', display)}?text={quote(text)}"


def _fail(run: Run, error: str, seller_text: str, *, emit: bool = True) -> None:
    run.state = "failed"
    run.error = error
    run.finished = time.time()
    _db_save(run)
    if emit:
        _emit(run, "run.failed", error)
    if seller_text:
        _say(run, run.owner_wa_id, seller_text)


# ------------------------------------------------------------------------------------------ WhatsApp

WELCOME = (
    "¡Hola! Soy Génesis. Te armo tu negocio en WhatsApp en minutos.\n\n"
    "1. Mándame una *nota de voz* contándome qué vendes, precios, horario y dónde estás.\n"
    "2. Mándame hasta *3 fotos* de tus productos.\n"
    "3. Escribe *listo*."
)


def _arm_timer(sess: Session) -> None:
    if sess.timer:
        sess.timer.cancel()
        sess.timer = None
    if sess.audio and sess.photos and AUTO_COMPILE_S > 0:
        t = threading.Timer(AUTO_COMPILE_S, flush, args=(sess.wa_id,))
        t.daemon = True
        sess.timer = t
        t.start()


def flush(wa_id: str) -> str | None:
    """Close the collection and start the run (called by "listo" or the 20 s timer; tests call it)."""
    sess = _session(wa_id)
    with sess.lock:
        if sess.timer:
            sess.timer.cancel()
            sess.timer = None
        current = RUNS.get(sess.run_id)
        if current is not None and current.state not in TERMINAL:
            return None
        if not sess.audio:
            return None
        audio, mime, photos = sess.audio, sess.mime, list(sess.photos)
        sess.audio, sess.photos = None, []
        _send(wa_id, "Dame un momentico, estoy escuchando tu nota de voz.")
        run_id = start_run(wa_id, audio, mime, photos, auto_confirm=False, via="whatsapp")
        sess.run_id = run_id
        return run_id


def _send(wa_id: str, text: str) -> None:
    try:
        _mod("whatsapp").send_text(_intake_phone(), wa_id, text)
    except Exception:  # noqa: BLE001
        log.exception("send_text failed")


def _download(media_id: str) -> tuple[bytes, str]:
    return _mod("whatsapp").download_media(media_id)


def _first_time(key: str) -> bool:
    """store.mark_seen with a namespaced key (agent.handle marks the bare message id)."""
    if key.endswith(":"):  # no message id: cannot dedupe
        return True
    try:
        mark_seen = getattr(_mod("store"), "mark_seen", None)
        return True if mark_seen is None else bool(mark_seen(key))
    except Exception:  # noqa: BLE001
        return True


def handle_intake(inbound: Any) -> None:
    """One inbound message on the intake number."""
    wa_id = inbound.wa_id
    if not _first_time("intake:" + (inbound.message_id or "")):
        return  # Meta retries webhooks: never count the same photo or voice note twice
    sess = _session(wa_id)
    try:
        _mod("whatsapp").mark_read(inbound.phone_id, inbound.message_id)
    except Exception:  # noqa: BLE001
        pass
    with sess.lock:
        if inbound.name:
            sess.name = inbound.name
        run = RUNS.get(sess.run_id) if sess.run_id else None
        if run is not None and run.state not in TERMINAL:
            _handle_in_run(run, inbound)
            return
        if run is not None and run.state == "live" and inbound.type in ("text", "interactive", "button") and not sess.audio:
            _send(wa_id, f"Tu negocio ya está vivo: {run.wa_me}\n\nSi quieres crear otro, mándame una nota de voz.")
            return
        _collect(sess, inbound)


def _collect(sess: Session, inbound: Any) -> None:
    wa_id = sess.wa_id
    kind = inbound.type
    if kind == "audio":
        try:
            data, mime = _download(inbound.media_id)
        except Exception:  # noqa: BLE001
            log.exception("audio download failed")
            _send(wa_id, "No pude descargar la nota de voz. ¿Me la mandas otra vez?")
            return
        replaced = sess.audio is not None
        sess.audio, sess.mime = data, (mime or inbound.mime or "audio/ogg")
        if replaced:
            _send(wa_id, "Listo, me quedo con esta nota de voz en vez de la anterior.")
        elif sess.photos:
            _send(wa_id, "¡Recibí tu nota de voz! Si ya mandaste todo, escribe *listo*.")
        else:
            _send(wa_id, "¡Recibí tu nota de voz! Ahora mándame hasta 3 fotos de tus productos y escribe *listo*.")
        _arm_timer(sess)
        return
    if kind == "image":
        if len(sess.photos) >= MAX_PHOTOS:
            _send(wa_id, "Ya tengo 3 fotos, con eso me alcanza. Escribe *listo* cuando quieras.")
            return
        try:
            data, _mime = _download(inbound.media_id)
        except Exception:  # noqa: BLE001
            log.exception("image download failed")
            _send(wa_id, "No pude descargar esa foto. ¿Me la mandas otra vez?")
            return
        sess.photos.append(data)
        n = len(sess.photos)
        if not sess.audio:
            _send(wa_id, f"Foto {n} de 3 recibida. Ahora mándame una nota de voz contándome de tu negocio.")
        else:
            _send(wa_id, f"Foto {n} de 3 recibida.")
        _arm_timer(sess)
        return
    text = inbound.text or ""
    if text and is_done(text):
        if sess.audio:
            flush(wa_id)
        else:
            _send(wa_id, "Primero mándame una nota de voz contándome de tu negocio: qué vendes, precios, horario y dónde estás.")
        return
    if sess.audio or sess.photos:
        _send(wa_id, "Cuando termines de mandar la nota de voz y las fotos, escribe *listo*.")
    else:
        _send(wa_id, WELCOME)


def _reply_text(inbound: Any) -> str:
    if inbound.type == "audio" and inbound.media_id:
        try:
            data, mime = _download(inbound.media_id)
            return _mod("muse").transcribe(data, mime or "audio/ogg").text
        except Exception:  # noqa: BLE001
            log.exception("answer transcription failed")
            return ""
    return inbound.text or ""


def _handle_in_run(run: Run, inbound: Any) -> None:
    wa_id = run.owner_wa_id
    if run.state in ("compiling", "building"):
        _send(wa_id, "Sigo trabajando en tu negocio, ya casi." if run.state == "building" else "Dame un momentico, ya casi.")
        return
    if run.state == "asking":
        text = _reply_text(inbound) or (inbound.button_id or "")
        if not text:
            _send(wa_id, run.question)
            return
        run.state = "compiling"
        _spawn(_refine, run, text)
        return
    if run.state == "confirming":
        if inbound.button_id == BTN_YES:
            confirm(run.run_id)
            return
        if inbound.button_id == BTN_EDIT:
            _say(run, wa_id, "Dime qué cambio y lo corrijo.")
            return
        text = _reply_text(inbound)
        if not text:
            _ask_confirm(run, "¿Lo creo así? Responde *sí* o dime qué cambio.")
            return
        if _mod("compiler").is_yes(text):
            _emit(run, "msg", text, who="seller")
            confirm(run.run_id)
            return
        run.state = "compiling"
        _spawn(_refine, run, text)

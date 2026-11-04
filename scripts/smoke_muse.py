"""One-shot check of every Meta Model API endpoint Génesis uses. Needs MODEL_API_KEY (never printed).

    uv run python scripts/smoke_muse.py [voice_note.ogg] [photo.jpg]

Prints latency and response shape per endpoint so the assumptions in genesis/muse.py can be confirmed
with a real key. Without a key it exits 0 after saying it skipped. Writes generated images to
media/smoke/ (gitignored).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GENESIS_MOCK", "0")

from genesis import muse  # noqa: E402
from genesis.config import settings  # noqa: E402

OUT = ROOT / "media" / "smoke"
results: list[tuple[str, bool, int, str]] = []


def shape(obj, depth: int = 0):
    """Structure of a JSON value with long strings truncated, to see field names without dumping payloads."""
    if depth > 4:
        return "…"
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1), f"(+{len(obj) - 1})"] if len(obj) > 1 else [shape(x, depth + 1) for x in obj]
    if isinstance(obj, str) and len(obj) > 120:
        return obj[:120] + f"… ({len(obj)} chars)"
    return obj


def step(name: str):
    def deco(fn):
        t0 = time.time()
        try:
            detail = fn()
            ok = True
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if os.getenv("SMOKE_TRACE"):
                traceback.print_exc()
            ok = False
        ms = int((time.time() - t0) * 1000)
        results.append((name, ok, ms, str(detail)))
        print(f"\n[{'OK ' if ok else 'ERR'}] {name}  {ms} ms\n{detail}", flush=True)
        return fn

    return deco


def sample_audio() -> bytes:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).read_bytes()
    # No voice note given: synthesize Spanish speech with espeak-ng if available, else a tone (ASR may return "").
    if shutil.which("espeak-ng"):
        wav = subprocess.run(["espeak-ng", "-v", "es-419", "--stdout", "Hola, soy Rosa y vendo arepas en San Fernando, a cinco mil pesos."],
                             capture_output=True).stdout
        if wav:
            return wav
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                           "-c:a", "libopus", "-f", "ogg", "pipe:1"], capture_output=True).stdout


def sample_photo() -> bytes:
    if len(sys.argv) > 2:
        return Path(sys.argv[2]).read_bytes()
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (640, 480), (250, 250, 250))
    ImageDraw.Draw(im).ellipse((200, 140, 440, 380), fill=(230, 170, 40), outline=(150, 90, 20), width=6)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def main() -> int:
    if not settings.model_api_key:
        print("MODEL_API_KEY not set: nothing to check (mock mode covers offline runs).")
        return 0
    if settings.mock:
        print("GENESIS_MOCK is on: unset it (or GENESIS_MOCK=0) to hit the real API.")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"base_url={settings.model_base_url} reasoning={settings.model_reasoning}")
    audio, photo = sample_audio(), sample_photo()
    wav = muse.to_wav16k(audio, "audio/wav" if audio[:4] == b"RIFF" else "audio/ogg")

    @step(f"POST /asr/transcribe ENDPOINTING ({settings.model_transcribe})")
    def _():
        return json.dumps(shape(muse.asr_raw(wav, "ENDPOINTING")), ensure_ascii=False, indent=1)

    @step("POST /asr/transcribe PUSH_TO_TALK")
    def _():
        return json.dumps(shape(muse.asr_raw(wav, "PUSH_TO_TALK")), ensure_ascii=False, indent=1)

    @step("muse.transcribe (full path)")
    def _():
        t = muse.transcribe(audio, "audio/wav" if audio[:4] == b"RIFF" else "audio/ogg")
        return f"model={t.model} segments={len(t.segments)} text={t.text[:200]!r}"

    @step(f"chat input_audio fallback ({settings.model_audio})")
    def _():
        import base64

        r = muse._client().chat.completions.create(model=settings.model_audio, messages=[{"role": "user", "content": [
            {"type": "text", "text": "Transcribe literalmente."},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}}]}])
        return repr((r.choices[0].message.content or "")[:200])

    @step(f"structured json_schema + image ({settings.model_spec})")
    def _():
        schema = {"type": "object", "properties": {"color": {"type": "string"}, "shape": {"type": "string"},
                                                   "price_cop": {"type": "integer"}}, "required": ["color", "shape", "price_cop"]}
        return json.dumps(muse.structured("Describe la figura de la foto y pon price_cop=5000.", schema, "probe", images=[photo]), ensure_ascii=False)

    @step("structured BusinessSpec from text")
    def _():
        from genesis.spec import BusinessSpec, json_schema

        fx = muse.load_fixture("rosa_transcript.json")
        out = muse.structured("Transcripción con segundos:\n" + "\n".join(f"[{s['start']}s] {s['text']}" for s in fx["segments"])
                              + "\nLlena el BusinessSpec. evidence.source_s = segundo donde se dijo.", json_schema(), "business_spec")
        spec = BusinessSpec.model_validate(out)
        return f"valid: {spec.business_name} / {[ (p.name, p.price_cop) for p in spec.products]} / hours={spec.hours}"

    @step("chat with tools (tool_calls shape)")
    def _():
        tools = [{"type": "function", "function": {"name": "crear_pedido", "description": "Crea un pedido",
                  "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
                      "product_id": {"type": "string"}, "qty": {"type": "integer"}}}}}, "required": ["items"]}}}]
        r = muse.chat([{"role": "system", "content": "Eres el asistente de Arepas Rosa. Productos: arepa-de-choclo-con-queso 5000."},
                       {"role": "user", "content": "quiero 2 arepas de choclo con queso"}], tools)
        return json.dumps(r, ensure_ascii=False)

    @step(f"POST /responses SAM ({settings.model_sam}) raw")
    def _():
        raw = muse.sam_raw(photo, "yellow circle")
        text = muse.responses_text(raw)
        return json.dumps(shape(raw), ensure_ascii=False, indent=1)[:2500] + f"\nparsed={muse.parse_sam_text(text, muse.image_size(photo))}"

    @step("muse.segment + crop")
    def _():
        boxes = muse.segment(photo, "yellow circle")
        if boxes:
            (OUT / "crop.png").write_bytes(muse.crop(photo, boxes[0]["box"]))
        return f"boxes={boxes} (expected ~[200,140,440,380])"

    @step(f"POST /images/generations ({settings.model_image})")
    def _():
        img = muse.image_generate("Logo plano y simple para 'Arepas Rosa', una arepa dorada, fondo crema, sin texto pequeño.", "1:1")
        (OUT / "logo.png").write_bytes(img)
        return f"{muse.sniff_mime(img)} {muse.image_size(img)} {len(img)} bytes -> media/smoke/logo.png"

    @step("POST /images/edits")
    def _():
        img = muse.image_edit(photo, "Deja el producto igual y pon un fondo blanco limpio de estudio.")
        (OUT / "edit.png").write_bytes(img)
        return f"{muse.sniff_mime(img)} {muse.image_size(img)} {len(img)} bytes -> media/smoke/edit.png"

    print("\n== summary ==")
    for name, ok, ms, _ in results:
        print(f"{'OK ' if ok else 'ERR'} {ms:>6} ms  {name}")
    return 0 if all(ok for _, ok, _, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

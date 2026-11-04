"""Stage rehearsal with the real Meta models and WhatsApp in dry-run (needs MODEL_API_KEY, no WA token).

    uv run python scripts/rehearse_real.py [voice_note.ogg] [photo1.jpg photo2.jpg]

Without files it builds stand-ins once under media/rehearsal/ (gitignored): Rosa's monologue spoken by
espeak-ng and two product photos drawn by muse-image-1.0. Every image the plan uploads (logo, menu,
cut-outs) is copied to media/rehearsal/out/ so you can look at what the models made.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["GENESIS_MOCK"] = "0"
os.environ["WA_DRY_RUN"] = "1"

from genesis import demo, muse, whatsapp  # noqa: E402
from genesis.config import settings  # noqa: E402

DIR = ROOT / "media" / "rehearsal"
PHOTO_PROMPTS = [
    "Foto casera tomada con celular de dos arepas de choclo con queso recién asadas sobre una hoja de plátano, "
    "en un puesto callejero de Cali, luz de mañana, sin texto.",
    "Foto casera tomada con celular de una arepa rellena de carne desmechada en un plato de plástico, sobre una "
    "mesa de madera de un puesto de arepas, luz natural, sin texto.",
]


def voice_note() -> tuple[bytes, str]:
    out = DIR / "rosa_tts.ogg"
    if not out.exists():
        if not shutil.which("espeak-ng"):
            sys.exit("falta espeak-ng (o pasa una nota de voz real como primer argumento)")
        text = muse.load_fixture("rosa_transcript.json")["text"]
        wav = subprocess.run(["espeak-ng", "-v", "es-419", "-s", "150", "--stdout", text], capture_output=True, check=True).stdout
        ogg = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "24k",
                              "-f", "ogg", "pipe:1"], input=wav, capture_output=True, check=True).stdout
        out.write_bytes(ogg)
    return out.read_bytes(), "audio/ogg"


def photos() -> list[bytes]:
    got = []
    for i, prompt in enumerate(PHOTO_PROMPTS, 1):
        p = DIR / f"arepa_{i}.png"
        if not p.exists():
            print(f"generando foto {i} con {settings.model_image}…", flush=True)
            p.write_bytes(muse.image_generate(prompt, "1:1"))
        got.append(_jpeg(p.read_bytes()))
    return got


def _jpeg(img: bytes) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(img)).convert("RGB").save(buf, "JPEG", quality=88)
    return buf.getvalue()


def main() -> int:
    if not settings.model_api_key:
        sys.exit("falta MODEL_API_KEY en .env")
    DIR.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    audio, mime = (Path(args[0]).read_bytes(), "audio/ogg") if args else voice_note()
    pics = [_jpeg(Path(a).read_bytes()) for a in args[1:3]] or photos()

    def echo(ln, t0):
        print(f"{int((ln.t - t0) * 1000):>7} ms  {ln.who:<22} {ln.text}", flush=True)

    try:
        work = DIR / "work"
        shutil.rmtree(work, ignore_errors=True)
        res = demo.run(work, echo=echo, real_models=True, inputs=(audio, mime, pics))
    except Exception as exc:  # noqa: BLE001
        print(f"\nENSAYO FALLÓ: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        out = DIR / "out"
        out.mkdir(exist_ok=True)
        n = 0
        for mid, (data, mt) in list(whatsapp.DRY_MEDIA.items()):
            if mt.startswith("image/") and not mid.startswith("rosa-"):
                n += 1
                (out / f"{n:02d}_{mid.replace('/', '_')}.{mt.split('/')[1].split(';')[0]}").write_bytes(data)
        for f in sorted((DIR / "work").rglob("*")):
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                n += 1
                shutil.copy(f, out / f"{n:02d}_{f.name}")
        print(f"\n{n} imágenes generadas por el plan -> {out}")

    print("\n== panel ==")
    for ln in sorted((x for x in res.lines if x.who == "panel"), key=lambda x: x.t):
        print(f"{int((ln.t - res.t0) * 1000):>7} ms  {ln.text}")
    order = res.paid or res.order
    print(f"\nrun_id   {res.run_id}\npregunta {res.question or '(ninguna)'}\ncuna     {res.cradle} {res.wa_me}")
    print(f"pedido   {order.get('id')} · ${order.get('total_cop', 0):,} · {order.get('status')}".replace(",", "."))
    print(f"total    {res.elapsed_ms} ms")
    return 0 if order.get("status") == "paid" else 1


if __name__ == "__main__":
    raise SystemExit(main())

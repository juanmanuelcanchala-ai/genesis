"""Shared helpers for the blueprint lanes.

Sibling modules (whatsapp, muse, flowgen, store, agent, payments, inspector) are resolved lazily through
`mod()` so the blueprint imports even while they are being written, and tests can swap them via
`sys.modules`. Settings are read through `cfg()` for the same reason (tests may replace them).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType
from typing import Any

from ..plan import Ctx

REPO_ROOT = Path(__file__).resolve().parents[2]

FOOD_WORDS = (
    "arepa", "empanada", "comida", "postre", "tamal", "jugo", "cafe", "café", "panader", "pan ", "restaur",
    "almuerzo", "buñuelo", "bunuelo", "pandebono", "torta", "pastel", "hamburg", "perro", "pizza", "salchipapa",
    "chuzo", "fritanga", "sancocho", "helado", "oblea", "cholado", "mazamorra", "lulada", "dulce", "galleta",
)


def mod(name: str) -> ModuleType:
    """Import `genesis.<name>` on demand (honours stubs placed in sys.modules)."""
    return importlib.import_module(f"genesis.{name}")


def cfg() -> Any:
    from .. import config

    return config.settings


def media_root() -> Path:
    return Path(os.getenv("GENESIS_MEDIA_DIR", str(REPO_ROOT / "media")))


def save_media(ctx: Ctx, name: str, data: bytes) -> tuple[str, str]:
    """Write bytes under media/<run_id>/<name>. Returns (url_path served by /media, local file path)."""
    folder = media_root() / ctx.run_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return f"/media/{ctx.run_id}/{name}", str(path)


def read_file(path: str | None) -> bytes | None:
    if not path:
        return None
    p = Path(path)
    return p.read_bytes() if p.is_file() else None


def detail(ctx: Ctx, lane: str, node: str, message: str, **data: Any) -> None:
    ctx.emit("node.detail", lane, node, message, **data)


def image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def image_ext(data: bytes) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[image_mime(data)]


def image_size(data: bytes) -> list[int] | None:
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return [im.width, im.height]
    except Exception:  # noqa: BLE001 - size is cosmetic panel data
        return None


def owner_wa_id(ctx: Ctx) -> str:
    """Seller's WhatsApp id. plan.Ctx has no field for it yet: onboarding sets `ctx.owner_wa_id`
    (dynamic attribute) or puts {"owner_wa_id": ...} under ctx.outputs["run"]."""
    return getattr(ctx, "owner_wa_id", "") or ctx.out("run").get("owner_wa_id", "") or ""


def display_number(ctx: Ctx) -> str:
    if ctx.cradle_display_number:
        return ctx.cradle_display_number
    s = cfg()
    if ctx.cradle_phone_id in s.cradle_phone_ids:
        i = s.cradle_phone_ids.index(ctx.cradle_phone_id)
        if i < len(s.cradle_display_numbers):
            return s.cradle_display_numbers[i]
    return ""


def cop(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


DAY_ES = {"mon": "lun", "tue": "mar", "wed": "mié", "thu": "jue", "fri": "vie", "sat": "sáb", "sun": "dom"}
DAY_ORDER = list(DAY_ES)


def hours_text(spec: Any) -> str:
    h = spec.hours
    if not h:
        return ""
    days = [d for d in DAY_ORDER if d in [x.lower()[:3] for x in h.days]]
    if not days:
        span = ""
    elif len(days) == 7:
        span = "todos los días"
    elif len(days) > 2 and DAY_ORDER.index(days[-1]) - DAY_ORDER.index(days[0]) == len(days) - 1:
        span = f"{DAY_ES[days[0]]} a {DAY_ES[days[-1]]}"
    else:
        span = ", ".join(DAY_ES[d] for d in days)
    return f"{span} {h.open}-{h.close}".strip()


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

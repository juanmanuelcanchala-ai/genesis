"""Lane "brand": SAM 3.1 product crops, Muse Image logo and a clean product shot (hero)."""

from __future__ import annotations

from typing import Any

from ..plan import Ctx, Node
from ..spec import BusinessSpec
from ._common import detail, image_ext, image_size, mod, read_file, save_media

LANE = "brand"


def _singular(word: str) -> str:
    w = word.strip().lower()
    return w[:-1] if len(w) > 4 and w.endswith(("as", "os")) else w


def _prompts(spec: BusinessSpec) -> list[str]:
    """SAM 3.1 grounds short nouns ("arepa") and misses long phrases ("Arepa de choclo con queso") and
    plurals, so try the singular head nouns first, then the full names, then a generic fallback."""
    cat = spec.category.strip()
    names = [p.name.strip() for p in spec.products[:2] if p.name.strip()]
    words = [_singular(cat.split()[0]) if cat else "", *(_singular(n.split()[0]) for n in names), cat, *names, "food"]
    return [w for i, w in enumerate(words) if w and w.lower() not in [x.lower() for x in words[:i]]]


def apply_crops(ctx: Ctx) -> dict[str, Any]:
    if not ctx.photos:
        detail(ctx, LANE, "brand.crops", "Sin fotos: la marca sale solo del audio")
        return {"crops": [], "skipped": "sin fotos"}
    muse = mod("muse")
    crops: list[dict[str, Any]] = []
    for i, photo in enumerate(ctx.photos):
        boxes: list[dict[str, Any]] = []
        used = ""
        for prompt in _prompts(ctx.spec):
            boxes = muse.segment(photo, prompt) or []
            used = prompt
            if boxes:
                break
        detail(ctx, LANE, "brand.crops", f"Foto {i + 1}: SAM 3.1 encontró {len(boxes)} «{used}»",
               photo=i, prompt=used, boxes=boxes, image_size=image_size(photo))
        if not boxes:
            continue
        best = max(boxes, key=lambda b: float(b.get("score", 0)))
        data = muse.crop(photo, best["box"])
        url, path = save_media(ctx, f"crop_{i}.{image_ext(data)}", data)
        crops.append({"photo": i, "box": best["box"], "score": float(best.get("score", 0)), "path": url, "file": path})
    detail(ctx, LANE, "brand.crops", f"{len(crops)} recorte(s) listos", crops=[{k: c[k] for k in ("photo", "box", "score", "path")} for c in crops])
    return {"crops": crops, "count": len(crops)}


def verify_files(ctx: Ctx, out: dict[str, Any]) -> bool:
    if out.get("skipped"):
        return True
    files = [c["file"] for c in out.get("crops", [])] if "crops" in out else [out.get("file")]
    return all(read_file(f) for f in files)


def logo_prompt(spec: BusinessSpec) -> str:
    products = ", ".join(p.name for p in spec.products[:3])
    return (
        f'Simple, friendly logo for a small Colombian street food business called "{spec.business_name}". '
        f"It sells {spec.category} ({products}) in {spec.neighborhood or spec.city}, {spec.city}. "
        "Flat vector style, warm colors, a single iconic illustration of the product, the business name written "
        "clearly and spelled exactly, centered on a plain light background, square, legible as a small round "
        "WhatsApp profile picture. No photo realism, no extra text."
    )


def apply_logo(ctx: Ctx) -> dict[str, Any]:
    prompt = logo_prompt(ctx.spec)
    data = mod("muse").image_generate(prompt, "1:1")
    url, path = save_media(ctx, f"logo.{image_ext(data)}", data)
    detail(ctx, LANE, "brand.logo", "Logo con Muse Image", image=url, prompt=prompt, bytes=len(data), size=image_size(data))
    return {"path": url, "file": path, "prompt": prompt, "bytes": len(data)}


HERO_PROMPT = (
    "Keep the food product exactly as it is (same shape, color, texture and portion; do not redraw it). "
    "Replace only the background with a clean, softly lit warm surface, like a simple menu photo. "
    "Center the product, square framing, no text, no hands, no extra objects."
)


def apply_hero(ctx: Ctx) -> dict[str, Any]:
    crops = ctx.out("brand.crops").get("crops") or []
    source = ""
    if crops:
        best = max(crops, key=lambda c: c.get("score", 0))
        base, source = read_file(best.get("file")), best.get("path", "")
    elif ctx.photos:
        base, source = ctx.photos[0], "foto 1"
    else:
        base = None
    if not base:
        return {"skipped": "sin fotos"}
    data = mod("muse").image_edit(base, HERO_PROMPT)
    url, path = save_media(ctx, f"hero.{image_ext(data)}", data)
    detail(ctx, LANE, "brand.hero", "Foto de producto con fondo limpio", image=url, source=source, size=image_size(data))
    return {"path": url, "file": path, "source": source}


def nodes(ctx: Ctx) -> list[Node]:
    return [
        Node("brand.crops", LANE, "Recorte con SAM 3.1", apply_crops, verify=verify_files, retries=1, critical=False),
        Node("brand.logo", LANE, "Logo con Muse Image", apply_logo, deps=["brand.crops"], verify=verify_files,
             retries=1, critical=False),
        Node("brand.hero", LANE, "Foto de producto", apply_hero, deps=["brand.crops"], verify=verify_files,
             retries=1, critical=False),
    ]

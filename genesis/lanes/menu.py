"""Lane "menu": catalog products (optional) and the WhatsApp Flow order form."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..plan import Ctx, Node, PlanError
from ._common import cfg, detail, display_number, mod, save_media

LANE = "menu"
FLOW_CATEGORIES = ["OTHER"]


def _public_url(path: str) -> str:
    return f"{cfg().public_base_url.rstrip('/')}{path}" if path.startswith("/") else path


def _product_image(ctx: Ctx, photo: int | None) -> str:
    crops = {c["photo"]: c["path"] for c in ctx.out("brand.crops").get("crops", [])}
    path = (photo is not None and crops.get(photo)) or ctx.out("brand.hero").get("path") or ctx.out("brand.logo").get("path") or ""
    return _public_url(path) if path else ""


def retailer_id(ctx: Ctx, product_id: str) -> str:
    return f"{ctx.spec.slug}-{product_id}"[:100]


def apply_catalog(ctx: Ctx) -> dict[str, Any]:
    catalog_id = cfg().catalog_id
    if not catalog_id:
        detail(ctx, LANE, "menu.catalog", "Sin catálogo configurado: el menú vive en el Flow")
        return {"skipped": "no catalog"}
    wa = mod("whatsapp")
    number = display_number(ctx)
    shop_url = wa.wa_me_link(number) if number else cfg().public_base_url
    created: list[dict[str, str]] = []
    try:
        for p in ctx.spec.products:
            rid = retailer_id(ctx, p.id)
            pid = wa.create_product(catalog_id, rid, p.name, p.description or p.name, p.price_cop,
                                    _product_image(ctx, p.photo), shop_url)
            created.append({"retailer_id": rid, "id": pid, "name": p.name})
    except Exception:
        for c in created:  # leave the catalog as we found it before the plan retries
            try:
                wa.delete_product(catalog_id, c["retailer_id"])
            except Exception:  # noqa: BLE001
                pass
        raise
    detail(ctx, LANE, "menu.catalog", f"{len(created)} producto(s) en el catálogo", products=created)
    return {"catalog_id": catalog_id, "products": created}


def revert_catalog(ctx: Ctx, out: dict[str, Any]) -> None:
    if out.get("skipped"):
        return
    wa = mod("whatsapp")
    for c in out.get("products", []):
        wa.delete_product(out["catalog_id"], c["retailer_id"])


def flow_name(ctx: Ctx) -> str:
    """Unique per run and per attempt: Meta wants Flow names unique within the WABA, and a reverted
    run leaves its Flow DEPRECATED (not deleted), still holding the name. Idempotency is State.key."""
    suffix = uuid.uuid4().hex[:10]
    return f"Pedido {ctx.spec.business_name[: 100 - len(suffix) - 8]} {suffix}"[:100]


def apply_flow(ctx: Ctx) -> dict[str, Any]:
    flowgen, wa = mod("flowgen"), mod("whatsapp")
    flow_json = flowgen.order_flow(ctx.spec)
    raw = json.dumps(flow_json, ensure_ascii=False, indent=2).encode()
    url, _ = save_media(ctx, "order_flow.json", raw)
    screens = [s.get("id") for s in flow_json.get("screens", [])]
    problems = flowgen.lint(flow_json)
    detail(ctx, LANE, "menu.flow", f"Flow JSON {len(raw):,} bytes".replace(",", ".") + f", {len(screens)} pantallas",
           bytes=len(raw), screens=screens, version=flow_json.get("version"), json=url, lint=problems)
    if problems:
        raise PlanError(f"Flow JSON no pasa el lint: {'; '.join(problems[:3])}")
    name = flow_name(ctx)
    flow_id = wa.create_flow(cfg().waba_id, name, FLOW_CATEGORIES)
    try:
        errors = wa.update_flow_json(flow_id, flow_json) or []
        detail(ctx, LANE, "menu.flow", "Meta validó el Flow" if not errors else f"Meta rechazó el Flow ({len(errors)} errores)",
               flow_id=flow_id, validation_errors=errors)
        if errors:
            first = errors[0]
            msg = (first.get("message") or first.get("error") or json.dumps(first, ensure_ascii=False)) if isinstance(first, dict) else str(first)
            raise PlanError(f"validation_errors del Flow: {msg}")
        try:
            wa.publish_flow(flow_id)
        except getattr(wa, "WhatsAppError", RuntimeError) as exc:
            # Meta can refuse to publish (e.g. no signed business public key on the number). The
            # business still works without the form: the agent takes orders in chat (crear_pedido).
            try:
                wa.deprecate_or_delete_flow(flow_id)
            except Exception:  # noqa: BLE001
                pass
            detail(ctx, LANE, "menu.flow", "Meta no dejó publicar el formulario: los pedidos se toman por chat",
                   flow_id=flow_id, error=str(exc))
            return {"flow_id": "", "name": name, "bytes": len(raw), "screens": screens, "json": url,
                    "published": False, "skipped": f"publish: {exc}"[:300]}
    except Exception:
        try:  # never leave a half-made Flow behind before the plan retries
            wa.deprecate_or_delete_flow(flow_id)
        except Exception:  # noqa: BLE001
            pass
        raise
    detail(ctx, LANE, "menu.flow", "Formulario de pedido publicado", flow_id=flow_id, name=name)
    return {"flow_id": flow_id, "name": name, "bytes": len(raw), "screens": screens, "json": url, "published": True}


def verify_flow(ctx: Ctx, out: dict[str, Any]) -> bool:
    if out.get("skipped"):  # published=False on purpose: orders go through the chat agent
        return True
    return bool(out.get("flow_id")) and bool(out.get("published"))


def revert_flow(ctx: Ctx, out: dict[str, Any]) -> None:
    if out.get("flow_id"):
        mod("whatsapp").deprecate_or_delete_flow(out["flow_id"])


def nodes(ctx: Ctx) -> list[Node]:
    return [
        Node("menu.catalog", LANE, "Catálogo", apply_catalog, deps=["brand.hero"], revert=revert_catalog,
             retries=1, critical=False),
        Node("menu.flow", LANE, "Formulario de pedido (Flow)", apply_flow, verify=verify_flow, revert=revert_flow),
    ]

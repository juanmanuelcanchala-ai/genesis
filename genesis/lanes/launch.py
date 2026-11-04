"""Lanes "inspector" and "launch": end-to-end test of the business, then switch it live and tell the seller."""

from __future__ import annotations

from typing import Any

from ..plan import Ctx, Node, PlanError
from ._common import cfg, detail, display_number, mod, owner_wa_id


def apply_inspector(ctx: Ctx) -> dict[str, Any]:
    report = mod("inspector").inspect(ctx.cradle_phone_id, ctx_emit=ctx.emit) or {}
    checks = report.get("checks", [])
    ok = sum(1 for c in checks if c.get("ok"))
    failed = [c for c in checks if not c.get("ok") and not c.get("warn")]
    detail(ctx, "inspector", "inspector.run", f"Inspector: {ok}/{len(checks)} pruebas bien", checks=checks,
           passed=bool(report.get("passed")))
    if not report.get("passed"):
        names = ", ".join(c.get("name", "?") for c in failed) or "sin detalle"
        raise PlanError(f"El inspector no aprobó: {names}")
    return {"passed": True, "checks": checks, "ok": ok, "total": len(checks)}


def live_message(ctx: Ctx, link: str) -> str:
    spec = ctx.spec
    hi = f"¡{spec.owner_name}, tu negocio está vivo!" if spec.owner_name else "¡Tu negocio está vivo!"
    lines = [
        hi,
        f"*{spec.business_name}* ya atiende por WhatsApp: tu asistente responde, toma pedidos y manda el link de pago.",
    ]
    if link:
        lines.append(f"Comparte este link con tus clientes: {link}")
    lines.append("Cada pedido te llega por aquí.")
    return "\n\n".join(lines)


def apply_live(ctx: Ctx) -> dict[str, Any]:
    store, wa = mod("store"), mod("whatsapp")
    store.set_live(ctx.cradle_phone_id, True)
    number = display_number(ctx)
    link = wa.wa_me_link(number, f"Hola {ctx.spec.business_name}, quiero hacer un pedido") if number else ""
    owner = owner_wa_id(ctx)
    msg_id, why = "", ""
    intake = cfg().intake_phone_id or ("" if cfg().wa_live else "intake")
    if owner and intake:
        # A notification must never revert a business that already passed the inspector.
        try:
            msg_id = wa.send_text(intake, owner, live_message(ctx, link)) or ""
        except Exception as exc:  # noqa: BLE001
            why = f"No pude avisar a la dueña por WhatsApp: {exc}"
    elif owner:
        why = "No pude avisar a la dueña: falta WA_INTAKE_PHONE_ID"
    if why:
        detail(ctx, "launch", "launch.live", why[:300])
    detail(ctx, "launch", "launch.live", "Tu negocio está vivo", wa_me=link, display_number=number,
           notified=bool(msg_id))
    return {"live": True, "wa_me": link, "display_number": number, "notified": msg_id}


def verify_live(ctx: Ctx, out: dict[str, Any]) -> bool:
    biz = mod("store").get_business(ctx.cradle_phone_id)
    return bool(biz and biz.get("live"))


def revert_live(ctx: Ctx, out: dict[str, Any]) -> None:
    mod("store").set_live(ctx.cradle_phone_id, False)


def nodes(ctx: Ctx) -> list[Node]:
    return [
        Node("inspector.run", "inspector", "Inspector (prueba de punta a punta)", apply_inspector,
             deps=["agent.config", "payments.ready", "number.profile"], retries=1),
        Node("launch.live", "launch", "¡En vivo!", apply_live, deps=["inspector.run"], verify=verify_live,
             revert=revert_live),
    ]

"""Lane "payments": make sure payment links can be created (Mercado Pago token valid, or mock)."""

from __future__ import annotations

from typing import Any

from ..plan import Ctx, Node, PlanError
from ._common import cfg, detail, mod

LANE = "payments"


def apply_ready(ctx: Ctx) -> dict[str, Any]:
    info = mod("payments").check() or {}
    if info.get("ok") is False:
        raise PlanError(f"Mercado Pago no responde: {info.get('error') or info.get('detail') or 'token inválido'}")
    s = cfg()
    provider = info.get("provider") or ("mercadopago" if s.mp_access_token and not s.mock else "mock")
    methods = [m.value for m in ctx.spec.payment_methods]
    detail(ctx, LANE, "payments.ready", f"Pagos con {'Mercado Pago' if provider == 'mercadopago' else 'link de prueba'}",
           provider=provider, methods=methods)
    if provider == "mercadopago" and not s.public_base_url.startswith("https://"):
        detail(ctx, LANE, "payments.ready", "PUBLIC_BASE_URL no es https: Mercado Pago no podrá avisar los pagos",
               public_base_url=s.public_base_url)
    public = {k: v for k, v in info.items() if isinstance(v, (str, int, float, bool)) and "token" not in k.lower()}
    return {**public, "provider": provider, "methods": methods}


def nodes(ctx: Ctx) -> list[Node]:
    return [Node("payments.ready", LANE, "Mercado Pago", apply_ready, retries=1)]

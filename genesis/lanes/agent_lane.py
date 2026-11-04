"""Lane "agent": persist the business (spec, Flow, system prompt) so agent.handle can serve it. Not live yet."""

from __future__ import annotations

from typing import Any

from ..plan import Ctx, Node, PlanError
from ._common import detail, mod, owner_wa_id

LANE = "agent"


def apply_config(ctx: Ctx) -> dict[str, Any]:
    agent, store = mod("agent"), mod("store")
    menu_flow = ctx.out("menu.flow")
    flow_id = menu_flow.get("flow_id", "")
    if not flow_id and not menu_flow.get("skipped"):
        raise PlanError("no hay flow_id de menu.flow")
    prompt = agent.system_prompt(ctx.spec)
    store.save_business(
        cradle_phone_id=ctx.cradle_phone_id,
        owner_wa_id=owner_wa_id(ctx),
        spec=ctx.spec,
        flow_id=flow_id,
        system_prompt=prompt,
        run_id=ctx.run_id,
    )
    store.set_live(ctx.cradle_phone_id, False)  # live only after the inspector passes
    tools = [t.get("function", t).get("name", "") for t in getattr(agent, "TOOLS", [])]
    detail(ctx, LANE, "agent.config", f"Agente listo: {len(ctx.spec.products)} productos, {len(tools)} herramientas",
           tools=tools, prompt_preview=prompt[:400], prompt_chars=len(prompt))
    return {"flow_id": flow_id, "tools": tools, "prompt_chars": len(prompt), "prompt_preview": prompt[:400]}


def verify_config(ctx: Ctx, out: dict[str, Any]) -> bool:
    biz = mod("store").get_business(ctx.cradle_phone_id)
    return bool(biz) and biz.get("flow_id") == out.get("flow_id")


def revert_config(ctx: Ctx, out: dict[str, Any]) -> None:
    store = mod("store")
    if hasattr(store, "delete_business"):
        store.delete_business(ctx.cradle_phone_id)
    else:
        store.set_live(ctx.cradle_phone_id, False)


def nodes(ctx: Ctx) -> list[Node]:
    return [Node("agent.config", LANE, "Agente del negocio", apply_config, deps=["menu.flow"],
                 verify=verify_config, revert=revert_config)]

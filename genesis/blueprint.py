"""Blueprint: BusinessSpec → the list of plan.Node that turns a cradle number into a live business.

Lanes (node ids, deps in brackets):
  number:    number.profile, number.photo [brand.logo]
  brand:     brand.crops, brand.logo [brand.crops], brand.hero [brand.crops]
  menu:      menu.catalog [brand.hero], menu.flow
  agent:     agent.config [menu.flow]
  payments:  payments.ready
  inspector: inspector.run [agent.config, payments.ready, number.profile]
  launch:    launch.live [inspector.run]
Brand, photo and catalog nodes are non-critical: they may fail without stopping the business.
"""

from __future__ import annotations

from . import plan
from .lanes import agent_lane, brand, launch, menu, number, payments_lane

LANES = (number, brand, menu, agent_lane, payments_lane, launch)


def build(ctx: plan.Ctx) -> list[plan.Node]:
    nodes: list[plan.Node] = []
    for lane in LANES:
        nodes.extend(lane.nodes(ctx))
    plan.toposort(nodes)  # fail fast on a wiring mistake
    return nodes

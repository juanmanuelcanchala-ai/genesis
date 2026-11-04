"""plan.apply_plan never reverts nodes served from State (they belong to an earlier, maybe live, run)."""

from __future__ import annotations

import pytest

from genesis import plan
from genesis.spec import BusinessSpec


def _spec() -> BusinessSpec:
    return BusinessSpec.model_validate({
        "business_name": "Arepas Rosa", "slug": "arepas-rosa", "category": "food",
        "products": [{"id": "arepa", "name": "Arepa", "price_cop": 5000}],
    })


def test_cached_nodes_are_not_reverted(tmp_path, monkeypatch):
    monkeypatch.setattr(plan.time, "sleep", lambda s: None)
    state = plan.State(str(tmp_path / "s.db"))
    reverted: list[str] = []
    fail = {"on": False}

    def ok(ctx):
        return {"x": 1}

    def maybe_fail(ctx):
        if fail["on"]:
            raise RuntimeError("inspector failed")
        return {"y": 1}

    def nodes():
        return [
            plan.Node("a", "menu", "A", ok, revert=lambda c, o: reverted.append("a"), retries=0),
            plan.Node("b", "inspector", "B", maybe_fail, deps=["a"], revert=lambda c, o: reverted.append("b"), retries=0),
        ]

    plan.apply_plan(plan.Ctx(run_id="r1", spec=_spec(), cradle_phone_id="c1"), nodes(), state)
    state.drop(nodes()[1].key(plan.Ctx(run_id="r1", spec=_spec(), cradle_phone_id="c1")))  # force b to rerun
    fail["on"] = True
    with pytest.raises(plan.PlanError):
        plan.apply_plan(plan.Ctx(run_id="r2", spec=_spec(), cradle_phone_id="c1"), nodes(), state)
    assert reverted == []  # "a" came from the cache: the earlier run's resource stays
    assert state.get(nodes()[0].key(plan.Ctx(run_id="r2", spec=_spec(), cradle_phone_id="c1"))) is not None

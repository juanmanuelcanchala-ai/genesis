"""End-to-end: the whole stage demo through webhooks, offline (mock models + WhatsApp dry-run)."""

from __future__ import annotations

import pytest

from genesis import demo, muse, onboarding, store
from genesis.config import settings

pytestmark = pytest.mark.skipif(not settings.mock or settings.wa_live, reason="needs GENESIS_MOCK=1 and WA_DRY_RUN=1")


@pytest.fixture(scope="module")
def res(tmp_path_factory):
    return demo.run(tmp_path_factory.mktemp("e2e"))


def _texts(res, wa_id, op="text"):
    return [demo._sent_text(s["op"], s["payload"]) for s in res.sent_to(wa_id, op)]


def test_intake_asks_one_question_then_confirms(res):
    rosa = _texts(res, demo.ROSA_WA_ID)
    assert "domicilio" in res.question
    assert sum(1 for t in rosa if t == res.question) == 1  # exactly ONE clarifying question
    buttons = _texts(res, demo.ROSA_WA_ID, "buttons")
    assert buttons and "Arepas Rosa" in buttons[0] and "Sí, créalo" in buttons[0]
    spec_events = [e for e in res.events if e.kind == "spec.ready" and e.run_id == res.run_id]
    assert len(spec_events) == 2  # first draft + after the answer
    assert spec_events[0].data["question"] and not spec_events[1].data["question"]
    assert spec_events[1].data["spec"]["fulfillment"] == "pickup"
    assert any(h["source_s"] is not None for h in spec_events[1].data["highlights"])


def test_every_lane_runs_and_inspector_approves(res):
    run_events = [e for e in res.events if e.run_id == res.run_id]
    done = {e.node for e in run_events if e.kind == "node.done"}
    assert done == {
        "number.profile", "number.photo", "brand.crops", "brand.logo", "brand.hero", "menu.catalog", "menu.flow",
        "agent.config", "payments.ready", "inspector.run", "launch.live",
    }
    assert {e.lane for e in run_events if e.kind == "node.done"} >= {"number", "brand", "menu", "agent", "payments", "inspector", "launch"}
    assert not [e for e in run_events if e.kind in ("node.failed", "run.failed", "node.reverted")]
    inspect_done = [e for e in run_events if e.kind == "inspect.done"]
    assert inspect_done and inspect_done[-1].data.get("passed", True)
    assert all(e.data.get("ok") for e in run_events if e.kind == "inspect.check")
    live = [e for e in run_events if e.kind == "run.live"]
    assert len(live) == 1 and live[0].data["wa_me"].startswith("https://wa.me/")
    ops = {s["op"] for s in res.sent}
    assert {"update_business_profile", "create_flow", "update_flow_json", "publish_flow"} <= ops


def test_business_is_live_and_rosa_gets_the_link(res):
    assert res.wa_me.startswith("https://wa.me/")
    rosa = _texts(res, demo.ROSA_WA_ID)
    assert any("tu negocio está vivo" in t.lower() and res.wa_me in t for t in rosa)


def test_customer_asks_price_orders_by_flow_and_pays(res):
    cust = [demo._sent_text(s["op"], s["payload"]) for s in res.sent_to(demo.CUSTOMER_WA_ID)]
    assert "asistente con IA" in cust[0]  # declares it is an AI on the first reply
    assert any("$5.000" in t for t in cust)  # price question
    assert res.sent_to(demo.CUSTOMER_WA_ID, "flow")  # order form offered
    pay = res.sent_to(demo.CUSTOMER_WA_ID, "cta_url")
    assert len(pay) == 1
    url = pay[0]["payload"]["interactive"]["action"]["parameters"]["url"]
    assert "/pay/mock/%23" in url
    assert res.order["total_cop"] == 15000 and res.order["pickup_time"] == "07:30"
    assert [i["qty"] for i in res.order["items"]] == [2, 1]
    assert res.paid["status"] == "paid" and res.paid["id"] == res.order["id"]
    assert any("recibimos tu pago" in t for t in cust)


def test_rosa_is_notified_of_order_and_payment(res):
    rosa = _texts(res, demo.ROSA_WA_ID)
    oid = res.order["id"]
    assert any(t.startswith("Nuevo pedido") and oid in t for t in rosa)
    assert any(t.startswith("Pagaron el pedido " + oid) for t in rosa)
    assert any(e.kind == "order.paid" and e.run_id == res.run_id for e in res.events)


def test_timeline_is_ordered_and_sandbox_restored(res):
    lines = res.timeline().splitlines()
    assert len(lines) > 30 and lines[0].lstrip().startswith("+")
    ms = [int(line.split("ms")[0].strip().lstrip("+")) for line in lines]
    assert ms == sorted(ms)
    assert onboarding.BACKGROUND is True and onboarding.DB_PATH is None
    assert "business_spec" not in muse.MOCK_STRUCTURED
    assert store._path is None  # store pointed back at the default DB

import json

import pytest

from genesis import flowgen
from genesis.spec import BusinessSpec


def rosa(**over) -> BusinessSpec:
    data = {
        "business_name": "Arepas Rosa",
        "owner_name": "Rosa",
        "category": "arepas",
        "neighborhood": "San Fernando",
        "products": [
            {"id": "arepa-de-choclo-con-queso", "name": "Arepa de choclo con queso", "price_cop": 5000},
            {"id": "arepa-de-carne-desmechada", "name": "Arepa de carne desmechada", "price_cop": 5000},
        ],
        "hours": {"days": ["mon", "tue", "wed", "thu", "fri", "sat"], "open": "06:00", "close": "11:00"},
        "fulfillment": "pickup",
    }
    data.update(over)
    return BusinessSpec.model_validate(data)


def _screen(flow, sid):
    return next(s for s in flow["screens"] if s["id"] == sid)


def _by_name(flow, sid):
    return {c.get("name"): c for c in _screen(flow, sid)["layout"]["children"] if c.get("name")}


def test_order_flow_is_valid_and_serializable():
    flow = flowgen.order_flow(rosa())
    assert flow["version"] == flowgen.FLOW_JSON_VERSION
    assert [s["id"] for s in flow["screens"]] == ["ORDER", "CONFIRM"]
    assert flowgen.lint(flow) == []
    json.dumps(flow, ensure_ascii=False)


def test_quantity_dropdowns_and_pickup_slots():
    flow = flowgen.order_flow(rosa())
    comps = _by_name(flow, "ORDER")
    qty = comps["qty_arepa_de_choclo_con_queso"]
    assert qty["type"] == "Dropdown"
    assert [o["id"] for o in qty["data-source"]] == [str(i) for i in range(11)]
    assert qty["init-value"] == "0"
    assert len(qty["label"]) <= 20
    slots = [o["id"] for o in comps["pickup_time"]["data-source"]]
    assert slots[0] == "06:00" and slots[-1] == "10:30" and len(slots) == 10
    assert comps["pickup_time"]["data-source"][0]["title"] == "6:00 a. m."
    assert "fulfillment" not in comps


def test_fulfillment_radio_only_when_both():
    flow = flowgen.order_flow(rosa(fulfillment="both", delivery_zone="San Fernando"))
    radio = _by_name(flow, "ORDER")["fulfillment"]
    assert radio["type"] == "RadioButtonsGroup"
    assert {o["id"] for o in radio["data-source"]} == {"pickup", "delivery"}
    assert flowgen.lint(flow) == []


def test_max_eight_products():
    products = [{"id": f"p{i}", "name": f"Producto número {i} muy largo", "price_cop": 1000 * (i + 1)} for i in range(12)]
    flow = flowgen.order_flow(rosa(products=products))
    qtys = [n for n in _by_name(flow, "ORDER") if n.startswith("qty_")]
    assert len(qtys) == 8
    assert flowgen.lint(flow) == []


def test_terminal_footer_complete_payload_and_navigate():
    flow = flowgen.order_flow(rosa())
    order_footer = _screen(flow, "ORDER")["layout"]["children"][-1]
    assert order_footer["on-click-action"]["name"] == "navigate"
    assert order_footer["on-click-action"]["next"] == {"type": "screen", "name": "CONFIRM"}
    confirm = _screen(flow, "CONFIRM")
    assert confirm["terminal"] is True
    footer = confirm["layout"]["children"][-1]
    assert footer["on-click-action"]["name"] == "complete"
    assert footer["on-click-action"]["payload"]["pickup_time"] == "${data.pickup_time}"


def test_overnight_and_missing_hours():
    s = rosa(hours={"days": ["fri", "sat"], "open": "20:00", "close": "01:00"})
    assert flowgen.pickup_slots(s) == ["20:00", "20:30", "21:00", "21:30", "22:00", "22:30", "23:00", "23:30", "00:00", "00:30"]
    s2 = rosa(hours=None)
    assert flowgen.pickup_slots(s2)[0] == "08:00"
    assert flowgen.lint(flowgen.order_flow(s2)) == []


def test_lint_catches_problems():
    bad = {
        "version": "7.3",
        "screens": [
            {"id": "order", "layout": {"type": "SingleColumnLayout", "children": [
                {"type": "Dropdown", "name": "a", "label": "x" * 25, "data-source": []},
                {"type": "TextInput", "name": "a", "label": "ok"},
                {"type": "TextBody", "text": "${data.nope}"},
                {"type": "Footer", "label": "Ir", "on-click-action": {"name": "navigate", "next": {"type": "screen", "name": "GONE"}, "payload": {}}},
            ]}},
            {"id": "SUCCESS", "layout": {"type": "SingleColumnLayout", "children": [{"type": "TextBody", "text": "hola"}]}},
        ],
    }
    errs = "\n".join(flowgen.lint(bad))
    for needle in ["uppercase", "reserved", "no terminal screen", "longer than 20", "data-source is empty",
                   "duplicate component name", "data.nope", "unknown screen"]:
        assert needle in errs, needle
    assert flowgen.lint({"version": "7.3", "screens": []})


def test_parse_order_uses_spec_prices():
    spec = rosa()
    resp = flowgen.example_response(spec, {"arepa-de-choclo-con-queso": 2, "arepa-de-carne-desmechada": 1})
    resp["unit_cop"] = "1"  # client-sent prices are ignored
    resp["price"] = 1
    order = flowgen.parse_order(resp, spec)
    assert order["total_cop"] == 15000
    assert order["items"][0] == {"product_id": "arepa-de-choclo-con-queso", "name": "Arepa de choclo con queso", "qty": 2, "unit_cop": 5000}
    assert order["pickup_time"] == "06:00"
    assert order["fulfillment"] == "pickup"
    assert order["customer_name"] == "Cliente de prueba"


def test_parse_order_rejects_empty_and_bad_quantities():
    spec = rosa()
    with pytest.raises(ValueError):
        flowgen.parse_order(flowgen.example_response(spec, {}), spec)
    with pytest.raises(ValueError):
        flowgen.parse_order({"qty_arepa_de_choclo_con_queso": "-3", "qty_arepa_de_carne_desmechada": "abc"}, spec)
    order = flowgen.parse_order({"qty_arepa_de_choclo_con_queso": "99"}, spec)
    assert order["items"][0]["qty"] == flowgen.MAX_QTY


def test_parse_order_fulfillment_both():
    spec = rosa(fulfillment="both")
    order = flowgen.parse_order(flowgen.example_response(spec, fulfillment="delivery"), spec)
    assert order["fulfillment"] == "delivery"
    order = flowgen.parse_order(flowgen.example_response(spec, fulfillment="teleport"), spec)
    assert order["fulfillment"] == "pickup"

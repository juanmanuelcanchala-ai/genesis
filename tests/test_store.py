import threading

import pytest

from genesis import store
from tests.e_support import CRADLE, OWNER, db, rosa  # noqa: F401


def test_business_roundtrip_and_live(db):
    spec = rosa()
    assert store.get_business(CRADLE) is None
    store.save_business(CRADLE, OWNER, spec, "flow-1", "prompt", "run-1")
    b = store.get_business(CRADLE)
    assert b["spec"] == spec and b["flow_id"] == "flow-1" and b["owner_wa_id"] == OWNER
    assert b["live"] is False and b["run_id"] == "run-1" and b["system_prompt"] == "prompt"
    store.set_live(CRADLE, True)
    store.save_business(CRADLE, OWNER, spec, "flow-2", "prompt2", "run-2")  # update keeps live flag
    b = store.get_business(CRADLE)
    assert b["live"] is True and b["flow_id"] == "flow-2"
    assert store.list_businesses()[0]["spec"].business_name == "Arepas Rosa"


def test_orders_ids_status_and_total_recomputed(db):
    items = [{"product_id": "arepa-de-choclo-con-queso", "name": "Arepa", "qty": 2, "unit_cop": 5000}]
    o1 = store.create_order(CRADLE, "c1", {"items": items, "total_cop": 1, "pickup_time": "07:00"})
    o2 = store.create_order(CRADLE, "c2", {"items": items}, test=True)
    assert (o1["id"], o2["id"]) == ("#1", "#2")
    assert o1["status"] == "pending" and o1["total_cop"] == 10000 and o1["pickup_time"] == "07:00"
    assert o2["test"] is True
    paid = store.set_order_status("#1", "paid", "pay-9")
    assert paid["status"] == "paid" and paid["payment_id"] == "pay-9"
    assert store.get_order("1")["status"] == "paid"  # "#" optional
    assert [o["id"] for o in store.list_orders(CRADLE)] == ["#2", "#1"]
    assert [o["id"] for o in store.list_orders(CRADLE, "c1")] == ["#1"]
    assert [o["id"] for o in store.list_orders(CRADLE, include_test=False)] == ["#1"]
    assert store.update_order("#1", pay_url="https://x")["pay_url"] == "https://x"
    with pytest.raises(KeyError):
        store.set_order_status("#99", "paid")


def test_history_and_seen(db):
    for i in range(5):
        store.append(CRADLE, "c1", "user" if i % 2 == 0 else "assistant", f"m{i}")
    store.append(CRADLE, "c2", "user", "other")
    h = store.history(CRADLE, "c1", limit=3)
    assert [m["content"] for m in h] == ["m2", "m3", "m4"]
    assert store.mark_seen("wamid.1") is True and store.mark_seen("wamid.1") is False


def test_thread_safe_order_creation(db):
    items = [{"product_id": "a", "name": "A", "qty": 1, "unit_cop": 1000}]
    threads = [threading.Thread(target=store.create_order, args=(CRADLE, f"c{i}", {"items": items})) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {o["id"] for o in store.list_orders(CRADLE)}
    assert len(ids) == 20

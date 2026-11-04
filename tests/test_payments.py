import json

import httpx
import respx

from genesis import payments, store, whatsapp
from tests.e_support import CRADLE, CUSTOMER, OWNER, db, rosa, save_rosa  # noqa: F401

ITEMS = [{"product_id": "arepa-de-choclo-con-queso", "name": "Arepa de choclo con queso", "qty": 2, "unit_cop": 5000}]


def _order(test=False):
    return store.create_order(CRADLE, CUSTOMER, {"items": ITEMS, "pickup_time": "07:00", "customer_name": "Carlos"}, test=test)


def test_money_and_summary():
    assert payments.money(5000) == "$5.000" and payments.money(1250000) == "$1.250.000"
    s = payments.order_summary({"id": "#3", "items": ITEMS, "total_cop": 10000, "pickup_time": "07:00", "fulfillment": "pickup"})
    assert "Pedido #3" in s and "2 x Arepa de choclo con queso" in s and "$10.000" in s and "recoge" in s


def test_mock_check_and_link(db):
    save_rosa()
    assert payments.check()["provider"] == "mock"
    o = _order()
    url = payments.link(o, rosa())
    assert url.endswith("/pay/mock/%231")
    assert store.get_order("#1")["pay_url"] == url


def test_mock_pay_notifies_owner_and_customer_once(db):
    save_rosa()
    o = _order()
    paid = payments.mock_pay(o["id"])
    assert paid["status"] == "paid" and paid["payment_id"] == "mock-1"
    texts = whatsapp.sent("text")
    to = {t["payload"]["to"] for t in texts}
    assert to == {OWNER, CUSTOMER}
    assert any("Pagaron el pedido #1" in t["payload"]["text"]["body"] for t in texts)
    payments.mock_pay(o["id"])  # idempotent
    assert len(whatsapp.sent("text")) == 2


def test_test_orders_do_not_notify(db):
    save_rosa()
    o = _order(test=True)
    assert payments.mock_pay(o["id"])["status"] == "paid"
    assert whatsapp.sent("text") == []


def test_webhook_mock_approved_rejected_and_amount_check(db):
    save_rosa()
    o = _order()
    payments.MOCK_PAYMENTS.clear()
    payments.MOCK_PAYMENTS["111"] = {"status": "rejected", "external_reference": o["id"], "transaction_amount": 10000}
    payments.MOCK_PAYMENTS["222"] = {"status": "approved", "external_reference": o["id"], "transaction_amount": 100}
    payments.MOCK_PAYMENTS["333"] = {"status": "approved", "external_reference": o["id"], "transaction_amount": 10000, "currency_id": "COP"}
    assert payments.handle_webhook({"type": "payment", "data": {"id": "111"}}, {}) is None
    assert payments.handle_webhook({"type": "payment", "data": {"id": "222"}}, {}) is None  # underpaid
    assert payments.handle_webhook({"type": "merchant_order", "data": {"id": "333"}}, {}) is None
    paid = payments.handle_webhook({}, {"topic": "payment", "id": "333"})  # legacy IPN shape
    assert paid["status"] == "paid" and paid["payment_id"] == "333"


def test_live_preference_and_webhook(db, monkeypatch):
    save_rosa()
    import dataclasses

    monkeypatch.setattr(payments, "_mock", lambda: False)
    monkeypatch.setattr(payments, "settings", dataclasses.replace(payments.settings, public_base_url="https://genesis.example.com"))
    o = _order(test=True)
    with respx.mock(assert_all_called=True) as mock:
        pref = mock.post("https://api.mercadopago.com/checkout/preferences").mock(
            return_value=httpx.Response(201, json={"id": "pref-1", "init_point": "https://www.mercadopago.com.co/checkout/v1/redirect?pref_id=pref-1", "sandbox_init_point": "https://sandbox.mercadopago.com.co/checkout/v1/redirect?pref_id=pref-1"})
        )
        url = payments.link(o, rosa())
        body = json.loads(pref.calls[0].request.content)
        assert url.startswith("https://sandbox.mercadopago.com.co")  # test order → sandbox
        assert body["external_reference"] == "#1"
        assert body["notification_url"].endswith("/mp/webhook")
        assert body["items"][0] == {"id": "arepa-de-choclo-con-queso", "title": "Arepa de choclo con queso", "quantity": 2, "unit_price": 5000, "currency_id": "COP"}
        assert set(body["back_urls"]) == {"success", "failure", "pending"}
        mock.get("https://api.mercadopago.com/v1/payments/987").mock(
            return_value=httpx.Response(200, json={"id": 987, "status": "approved", "external_reference": "#1", "transaction_amount": 10000, "currency_id": "COP"})
        )
        paid = payments.handle_webhook({"action": "payment.updated", "type": "payment", "data": {"id": "987"}}, {"data.id": "987", "type": "payment"})
        assert paid["status"] == "paid" and paid["payment_id"] == "987"
        assert store.get_order("#1")["preference_id"] == "pref-1"


def test_concurrent_notifications_notify_once(db):
    """MP sends IPN + Webhook almost together; both see the order pending but only one notifies."""
    import threading

    save_rosa()
    o = _order()
    stale = store.get_order(o["id"])  # both threads read it while still pending
    threads = [threading.Thread(target=payments._mark_paid, args=(dict(stale), "pay-1")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get_order(o["id"])["status"] == "paid"
    assert len(whatsapp.sent("text")) == 2  # one to the owner, one to the customer


def test_preference_skips_private_notification_url(db, monkeypatch):
    import dataclasses

    save_rosa()
    o = _order()
    for base, public in (("http://localhost:8000", False), ("http://demo.example.com", False),
                         ("https://genesis.example.com", True)):
        monkeypatch.setattr(payments, "settings", dataclasses.replace(payments.settings, public_base_url=base))
        body = payments.preference_body(o, rosa())
        assert ("notification_url" in body) is public
        if public:
            assert body["notification_url"] == "https://genesis.example.com/mp/webhook"

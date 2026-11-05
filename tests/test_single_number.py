"""Single-number mode: intake and cradle are the same WhatsApp number (e.g. one Meta test number)."""

from dataclasses import replace
from types import SimpleNamespace

from genesis import server
from genesis.whatsapp import Inbound

NUM, OWNER, CUSTOMER = "111", "573101234567", "573209876543"


def _setup(monkeypatch, biz):
    calls = []
    monkeypatch.setattr(server, "settings", replace(server.settings, intake_phone_id=NUM, cradle_phone_ids=[NUM]))
    monkeypatch.setattr(server.onboarding, "handle_intake", lambda i: calls.append(("intake", i.wa_id)))
    monkeypatch.setattr(server, "_customer_event", lambda i: None)
    fake = {"store": SimpleNamespace(get_business=lambda pid: biz),
            "agent": SimpleNamespace(handle=lambda pid, i: calls.append(("agent", i.wa_id)))}
    monkeypatch.setattr(server, "_mod", lambda name: fake[name])
    return calls


def _msg(wa_id, mid):
    return Inbound(phone_id=NUM, wa_id=wa_id, name="x", message_id=mid, type="text", text="hola")


def test_before_live_everyone_goes_to_genesis(monkeypatch):
    calls = _setup(monkeypatch, None)
    assert server.route_inbound(_msg(OWNER, "m1")) == "intake"
    assert server.route_inbound(_msg(CUSTOMER, "m2")) == "intake"


def test_after_live_customers_reach_the_business_and_owner_keeps_genesis(monkeypatch):
    calls = _setup(monkeypatch, {"live": True, "owner_wa_id": OWNER})
    assert server.route_inbound(_msg(CUSTOMER, "m3")) == "agent"
    assert server.route_inbound(_msg(OWNER, "m4")) == "intake"
    assert calls == [("agent", CUSTOMER), ("intake", OWNER)]


def test_two_numbers_unchanged(monkeypatch):
    calls = _setup(monkeypatch, {"live": True, "owner_wa_id": OWNER})
    monkeypatch.setattr(server, "settings", replace(server.settings, intake_phone_id=NUM, cradle_phone_ids=["222"]))
    assert server.route_inbound(_msg(CUSTOMER, "m5")) == "intake"

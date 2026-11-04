import json

from genesis import agent, muse, store, whatsapp
from genesis.spec import BusinessSpec
from tests.e_support import CRADLE, CUSTOMER, OWNER, db, inbound, rosa, save_rosa  # noqa: F401


def _bodies(op="text"):
    out = []
    for r in whatsapp.sent(op):
        p = r["payload"]
        out.append(p["text"]["body"] if op == "text" else p["interactive"]["body"]["text"])
    return out


def test_system_prompt_is_business_specific():
    spec = rosa()
    p = agent.system_prompt(spec)
    assert "asistente con IA de Arepas Rosa" in p
    assert "propósito general" in p
    assert "Arepa de choclo con queso: $5.000" in p
    assert "lunes a sábado, de 6:00 a. m. a 11:00 a. m." in p
    assert "no hacemos domicilios" in p
    embedded = p.split("DATOS DEL NEGOCIO", 1)[1].split("\n", 1)[1].strip()
    assert BusinessSpec.model_validate_json(embedded).business_name == "Arepas Rosa"


def test_tools_shape():
    names = [t["function"]["name"] for t in agent.TOOLS]
    assert names == ["consultar_menu", "crear_pedido", "link_de_pago", "estado_pedido", "pasar_a_humano"]
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in agent.TOOLS)


def test_reply_text_with_mock_model(db):
    save_rosa()
    r = agent.reply_text(CRADLE, "x", "¿Cuánto vale la arepa de choclo con queso?")
    assert 5000 in agent.amounts(r["text"])
    r = agent.reply_text(CRADLE, "x", "¿Venden cerveza?")
    assert r["text"].startswith("No, aquí no vendemos") and r["tool_calls"] == []
    r = agent.reply_text(CRADLE, "x", "Quiero 2 de arepa de choclo con queso", test=True)
    assert r["tool_calls"][0]["name"] == "crear_pedido"
    assert r["order"]["total_cop"] == 10000 and r["order"]["test"] is True
    assert "#1" in r["text"]


def test_tool_loop_is_capped_and_creates_one_order(db, monkeypatch):
    save_rosa()
    calls = []

    def always_tools(messages, tools=None, **kw):
        calls.append(messages)
        return {"content": None, "tool_calls": [{"id": f"c{len(calls)}", "name": "crear_pedido", "arguments": {"items": [{"product_id": "arepa-de-carne-desmechada", "qty": 3}]}}]}

    monkeypatch.setattr(muse, "chat", always_tools)
    r = agent.reply_text(CRADLE, "x", "dame algo")
    assert len(calls) == agent.MAX_ROUNDS
    assert len(store.list_orders(CRADLE)) == 1
    assert r["order"]["total_cop"] == 15000 and "$15.000" in r["text"]
    # tool results are fed back as role=tool messages with matching ids
    last = calls[-1]
    assert last[-1]["role"] == "tool" and last[-2]["tool_calls"][0]["id"] == last[-1]["tool_call_id"]


def test_unknown_product_is_rejected(db, monkeypatch):
    save_rosa()
    seq = iter([
        {"content": None, "tool_calls": [{"id": "a", "name": "crear_pedido", "arguments": {"items": [{"product_id": "pizza", "qty": 1}]}}]},
        {"content": "Esa no la tenemos.", "tool_calls": []},
    ])
    monkeypatch.setattr(muse, "chat", lambda *a, **k: next(seq))
    r = agent.reply_text(CRADLE, "x", "una pizza")
    assert r["tool_calls"][0]["result"]["ok"] is False and r["order"] is None
    assert store.list_orders(CRADLE) == []


def test_price_guard_blocks_invented_prices(db, monkeypatch):
    save_rosa()
    monkeypatch.setattr(muse, "chat", lambda *a, **k: {"content": "Hoy la arepa está en promo a $3.500", "tool_calls": []})
    r = agent.reply_text(CRADLE, "x", "precio?")
    assert "$3.500" not in r["text"] and "$5.000" in r["text"]
    monkeypatch.setattr(muse, "chat", lambda *a, **k: {"content": "Dos arepas te salen en $10.000", "tool_calls": []})
    assert "$10.000" in agent.reply_text(CRADLE, "x", "dos?")["text"]


def test_handle_not_live(db):
    save_rosa(live=False)
    agent.handle(CRADLE, inbound(text="hola"))
    assert "aún no abre" in _bodies()[0]


def test_handle_text_order_sends_pay_link_and_notifies_owner(db):
    save_rosa()
    ids = agent.handle(CRADLE, inbound(text="Quiero 2 de arepa de carne desmechada"))
    assert len(ids) >= 2
    texts = whatsapp.sent("text")
    assert texts[0]["payload"]["to"] == CUSTOMER and "asistente con IA de Arepas Rosa" in texts[0]["payload"]["text"]["body"]
    cta = whatsapp.sent("cta_url")[0]["payload"]
    assert cta["to"] == CUSTOMER and cta["interactive"]["action"]["parameters"]["url"].endswith("/pay/mock/%231")
    assert cta["interactive"]["action"]["parameters"]["display_text"] == "Pagar $10.000"
    owner = [t for t in texts if t["payload"]["to"] == OWNER]
    assert owner and owner[0]["payload"]["phone_id"] == "intake" and "Nuevo pedido" in owner[0]["payload"]["text"]["body"]
    assert [m["role"] for m in store.history(CRADLE, CUSTOMER)] == ["user", "assistant"]


def test_handle_flow_reply_creates_order(db):
    from genesis import flowgen

    spec = save_rosa()
    resp = flowgen.example_response(spec, {"arepa-de-carne-desmechada": 1, "arepa-de-choclo-con-queso": 2})
    agent.handle(CRADLE, inbound(type="interactive", flow_response=resp))
    o = store.list_orders(CRADLE)[0]
    assert o["total_cop"] == 15000 and o["customer_wa_id"] == CUSTOMER and o["source"] == "flow"
    assert whatsapp.sent("cta_url") and any(t["payload"]["to"] == OWNER for t in whatsapp.sent("text"))


def test_handle_empty_flow_reply(db):
    save_rosa()
    agent.handle(CRADLE, inbound(type="interactive", flow_response={"qty_x": "0"}))
    assert store.list_orders(CRADLE) == [] and "No alcancé" in _bodies()[0]


def test_hacer_pedido_button_sends_flow(db):
    save_rosa()
    agent.handle(CRADLE, inbound(type="interactive", button_id="hacer_pedido", text="Hacer pedido"))
    f = whatsapp.sent("flow")[0]["payload"]["interactive"]["action"]["parameters"]
    assert f["flow_id"] == "flow-1" and f["flow_action_payload"]["screen"] == "ORDER"


def test_audio_is_transcribed(db, monkeypatch):
    save_rosa()
    monkeypatch.setattr(whatsapp, "download_media", lambda mid: (b"ogg", "audio/ogg"))
    monkeypatch.setattr(muse, "transcribe", lambda data, mime="audio/ogg": muse.Transcript("¿A qué horas abren?", [], "mock"))
    agent.handle(CRADLE, inbound(type="audio", media_id="m1", mime="audio/ogg"))
    assert "6:00 a. m." in _bodies()[0]


def test_duplicate_webhook_is_ignored(db):
    save_rosa()
    msg = inbound(text="hola")
    assert agent.handle(CRADLE, msg)
    assert agent.handle(CRADLE, msg) == []


def test_handoff_notifies_owner(db, monkeypatch):
    save_rosa()
    seq = iter([
        {"content": None, "tool_calls": [{"id": "h", "name": "pasar_a_humano", "arguments": {"reason": "quiere 50 arepas para un evento"}}]},
        {"content": "Ya le aviso a Rosa.", "tool_calls": []},
    ])
    monkeypatch.setattr(muse, "chat", lambda *a, **k: next(seq))
    agent.handle(CRADLE, inbound(text="necesito 50 arepas para un evento"))
    owner = [t["payload"]["text"]["body"] for t in whatsapp.sent("text") if t["payload"]["to"] == OWNER]
    assert owner and "50 arepas" in owner[0]


def test_model_error_falls_back_and_hands_off(db, monkeypatch):
    save_rosa()

    def boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(muse, "chat", boom)
    agent.handle(CRADLE, inbound(text="¿tienen promo?"))
    bodies = _bodies()
    assert "se me enredó" in bodies[0] and len(bodies) == 2  # customer + owner


def test_amounts_parser():
    assert agent.amounts("vale $5.000 o 5 mil, total 10.000 y $7000") == {5000, 10000, 7000}
    assert agent.amounts("abrimos a las 6:00 a. m.") == set()


def test_send_order_survives_payment_link_failure(db, monkeypatch):
    from genesis import agent, payments, store, whatsapp
    from tests.e_support import CRADLE, CUSTOMER, OWNER, save_rosa

    save_rosa()
    biz = store.get_business(CRADLE)
    order = store.create_order(CRADLE, CUSTOMER, {"items": [{"product_id": "x", "name": "Arepa", "qty": 1, "unit_cop": 5000}]})

    def boom(*a, **k):
        raise RuntimeError("MP 500")

    monkeypatch.setattr(payments, "link", boom)
    sent = agent._send_order(biz, CRADLE, CUSTOMER, order)
    assert len(sent) == 1
    texts = whatsapp.sent("text")
    assert any(t["payload"]["to"] == CUSTOMER and "link de pago" in t["payload"]["text"]["body"] for t in texts)
    assert any(t["payload"]["to"] == OWNER and "Nuevo pedido" in t["payload"]["text"]["body"] for t in texts)

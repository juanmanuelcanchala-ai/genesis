from genesis import inspector, muse, store
from tests.e_support import CRADLE, db, rosa, save_rosa  # noqa: F401


def test_inspector_passes_on_rosa_and_emits_checks(db):
    save_rosa(live=False)
    events = []
    report = inspector.inspect(CRADLE, lambda kind, lane, node, message, **data: events.append((kind, lane, node, message, data)))
    failed = [c for c in report["checks"] if not c["ok"]]
    assert report["passed"], failed
    names = [c["name"] for c in report["checks"]]
    assert names == [
        "Se presenta como IA del negocio", "Precio", "Horario", "Entrega", "No vende lo prohibido",
        "Toma pedidos", "Pedido de prueba por el formulario", "Link de pago", "Pago de prueba",
    ]
    assert all(set(c) >= {"name", "ok", "detail"} for c in report["checks"])
    kinds = [e[0] for e in events]
    assert kinds[0] == "inspect.started" and kinds[-1] == "inspect.done" and kinds.count("inspect.check") == len(names)
    assert all(e[1] == "inspector" and e[2] == "inspector.run" for e in events)
    # every order the inspector made is a test order and the flow one got paid
    orders = store.list_orders(CRADLE)
    assert orders and all(o["test"] for o in orders)
    assert any(o["status"] == "paid" for o in orders)


def test_inspector_fails_when_agent_invents_or_sells_beer(db, monkeypatch):
    save_rosa(live=False)
    monkeypatch.setattr(muse, "chat", lambda *a, **k: {"content": "Sí claro, de todo y a buen precio.", "tool_calls": []})
    report = inspector.inspect(CRADLE)
    by = {c["name"]: c for c in report["checks"]}
    assert report["passed"] is False
    assert by["Precio"]["ok"] is False and by["Horario"]["ok"] is False and by["Toma pedidos"]["ok"] is False
    assert by["No vende lo prohibido"]["ok"] is True  # deterministic policy guard answers before the model


def test_inspector_without_business(db):
    report = inspector.inspect("nope")
    assert report["passed"] is False and report["checks"][0]["name"] == "Negocio guardado"


def test_inspector_broken_emit_does_not_break(db):
    save_rosa(live=False)

    def bad_emit(*a, **k):
        raise RuntimeError("panel down")

    assert inspector.inspect(CRADLE, bad_emit)["passed"] is True


def test_inspector_cash_only_business(db):
    from genesis.spec import PaymentMethod

    spec = rosa()
    spec.payment_methods = [PaymentMethod.cash]
    save_rosa(live=False, spec=spec)
    report = inspector.inspect(CRADLE)
    assert report["passed"] and report["checks"][-1]["name"] == "Pago"


def test_one_soft_miss_is_a_warning_not_a_block(db, monkeypatch):
    save_rosa(live=False)
    real = inspector.QUESTIONS
    never = lambda spec, res: (False, "no lo entendí")  # noqa: E731
    monkeypatch.setattr(inspector, "QUESTIONS", [(n, q, never if n == "Horario" else j) for n, q, j in real])
    report = inspector.inspect(CRADLE)
    by = {c["name"]: c for c in report["checks"]}
    assert report["passed"] is True and by["Horario"]["ok"] is False and by["Horario"]["warn"] is True
    monkeypatch.setenv("GENESIS_INSPECTOR_STRICT", "1")
    assert inspector.inspect(CRADLE)["passed"] is False


def test_price_in_words():
    spec = rosa()
    ok, _ = inspector._check_price(spec, {"text": "Vale cinco mil pesitos", "tool_calls": []})
    assert ok is (spec.products[0].price_cop == 5000)

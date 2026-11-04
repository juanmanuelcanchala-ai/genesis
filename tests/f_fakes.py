"""Stubs of sibling modules (compiler, blueprint, whatsapp, store, payments, agent, muse) for module F tests.

They follow docs/CONTRACTS.md signatures and are injected through sys.modules, so the F tests run
whether or not the real modules exist yet.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from genesis import plan
from genesis.spec import BusinessSpec, Evidence, Hours, Product


def rosa_spec(**kw: Any) -> BusinessSpec:
    data: dict[str, Any] = dict(
        business_name="Arepas Rosa",
        owner_name="Rosa",
        category="arepas",
        neighborhood="San Fernando",
        products=[
            Product(id="choclo", name="Arepa de choclo con queso", price_cop=5000, evidence=Evidence(source_s=4.0, quote="choclo con queso")),
            Product(id="carne", name="Arepa de carne desmechada", price_cop=5000, evidence=Evidence(source_s=7.5, quote="carne desmechada")),
        ],
        hours=Hours(days=["mon", "tue", "wed", "thu", "fri", "sat"], open="06:00", close="11:00", evidence=Evidence(source_s=10.0, quote="de seis a once")),
        evidence={"business_name": Evidence(source_s=1.2, quote="Arepas Rosa"), "neighborhood": Evidence(source_s=12.0, quote="San Fernando")},
    )
    data.update(kw)
    return BusinessSpec(**data)


@dataclass
class Draft:
    spec: BusinessSpec
    transcript: Any
    question: str = ""
    blocked: str | None = None


@dataclass
class Inbound:
    phone_id: str
    wa_id: str
    name: str = "Rosa"
    message_id: str = "wamid.1"
    type: str = "text"
    text: str = ""
    media_id: str = ""
    mime: str = ""
    button_id: str = ""
    flow_response: dict | None = None
    timestamp: int = 0


@dataclass
class Fakes:
    sent: list[dict] = field(default_factory=list)
    compile_calls: list[dict] = field(default_factory=list)
    built: list[Any] = field(default_factory=list)
    agent_calls: list[Any] = field(default_factory=list)
    paid: list[str] = field(default_factory=list)
    mp_calls: list[tuple] = field(default_factory=list)
    question: str = ""
    blocked: str | None = None
    fail_node: bool = False
    businesses: dict[str, dict] = field(default_factory=dict)


def install(monkeypatch: Any) -> Fakes:
    f = Fakes()

    # compiler
    comp = types.ModuleType("genesis.compiler")
    transcript = SimpleNamespace(
        text="Hola, soy Rosa, de Arepas Rosa. Vendo arepa de choclo con queso a cinco mil y de carne desmechada.",
        segments=[{"start": 0.0, "end": 6.0, "text": "Hola, soy Rosa, de Arepas Rosa. Vendo arepa de choclo con queso"},
                  {"start": 6.0, "end": 14.0, "text": "a cinco mil y de carne desmechada, de seis a once en San Fernando."}],
        model="mock",
    )

    def compile_spec(audio: bytes, mime: str, photos: list[bytes], answer: str = "", previous: Any = None) -> Draft:
        f.compile_calls.append({"audio": audio, "mime": mime, "photos": len(photos), "answer": answer, "previous": previous})
        q = "" if answer else f.question
        return Draft(rosa_spec(), transcript, q, f.blocked)

    comp.compile_spec = compile_spec
    comp.summary = lambda spec: f"*{spec.business_name}*\n- Arepa de choclo $5.000\n¿Lo creo así? Responde *sí*."
    comp.is_yes = lambda text: text.strip().lower().rstrip("!.") in ("si", "sí", "dale", "listo", "ok", "de una")
    monkeypatch.setitem(sys.modules, "genesis.compiler", comp)

    # blueprint
    bp = types.ModuleType("genesis.blueprint")

    def build(ctx: plan.Ctx) -> list[plan.Node]:
        f.built.append(ctx)

        def logo(c: plan.Ctx) -> dict:
            c.emit("node.detail", "brand", "brand.logo", "Logo listo", url=f"/media/{c.run_id}/logo.png")
            return {"url": f"/media/{c.run_id}/logo.png"}

        def bad(c: plan.Ctx) -> dict:
            raise RuntimeError("graph api down")

        nodes = [
            plan.Node("number.profile", "number", "Perfil del número", lambda c: {"about": c.spec.business_name}),
            plan.Node("brand.logo", "brand", "Logo", logo, critical=False),
            plan.Node("inspector.run", "inspector", "Inspector", lambda c: {"passed": True, "checks": [{"name": "precio", "ok": True, "detail": ""}]},
                      deps=["number.profile"]),
            plan.Node("launch.live", "launch", "Encender", lambda c: {"live": True}, deps=["inspector.run"]),
        ]
        if f.fail_node:
            nodes.append(plan.Node("menu.flow", "menu", "Flow", bad, retries=0))
        return nodes

    bp.build = build
    monkeypatch.setitem(sys.modules, "genesis.blueprint", bp)

    # whatsapp
    wa = types.ModuleType("genesis.whatsapp")
    wa.SENT = f.sent

    def rec(kind: str):
        def send(phone_id: str, to: str, body: str, *args: Any, **kw: Any) -> str:
            f.sent.append({"type": kind, "phone_id": phone_id, "to": to, "body": body, "args": args})
            return f"wamid.out{len(f.sent)}"
        return send

    wa.send_text = rec("text")
    wa.send_buttons = rec("buttons")
    wa.mark_read = lambda phone_id, message_id: None
    wa.download_media = lambda media_id: (b"IMG" if media_id.startswith("img") else b"OGG", "image/jpeg" if media_id.startswith("img") else "audio/ogg")
    wa.wa_me_link = lambda display, text="": f"https://wa.me/{display}" + ("?text=" + text.replace(" ", "%20") if text else "")
    wa.verify_signature = lambda raw, header: header != "sha256=bad"
    wa.parse_webhook = lambda payload: [Inbound(**m) for m in payload.get("fake", [])]
    monkeypatch.setitem(sys.modules, "genesis.whatsapp", wa)

    # store
    st = types.ModuleType("genesis.store")
    st.get_business = lambda cid: f.businesses.get(cid)
    st.list_orders = lambda cid: [{"id": "#1"}]
    monkeypatch.setitem(sys.modules, "genesis.store", st)

    # agent
    ag = types.ModuleType("genesis.agent")
    ag.handle = lambda cid, inbound: f.agent_calls.append((cid, inbound)) or []
    monkeypatch.setitem(sys.modules, "genesis.agent", ag)

    # payments
    pay = types.ModuleType("genesis.payments")

    def mock_pay(order_id: str) -> dict:
        f.paid.append(order_id)
        return {"id": order_id, "cradle_phone_id": "cradle-1", "total_cop": 10000, "status": "paid"}

    def handle_webhook(payload: dict, query: dict) -> dict | None:
        f.mp_calls.append((payload, query))
        return None

    pay.mock_pay = mock_pay
    pay.handle_webhook = handle_webhook
    monkeypatch.setitem(sys.modules, "genesis.payments", pay)

    # muse
    mu = types.ModuleType("genesis.muse")
    mu.transcribe = lambda audio, mime="audio/ogg": SimpleNamespace(text="hasta las once", segments=[], model="mock")
    monkeypatch.setitem(sys.modules, "genesis.muse", mu)
    return f

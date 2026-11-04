"""Blueprint + lanes: run plan.apply_plan on Rosa's spec against in-memory stubs of every sibling module."""

from __future__ import annotations

import dataclasses
import io
import json
import sys
import types
from pathlib import Path

import pytest

import genesis
from genesis import blueprint, config, plan
from genesis.events import bus
from genesis.spec import BusinessSpec

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "rosa_spec.json"

ROSA = {
    "business_name": "Arepas Rosa",
    "owner_name": "Rosa",
    "category": "arepas",
    "city": "Cali",
    "neighborhood": "San Fernando",
    "products": [
        {"id": "arepa-de-choclo-con-queso", "name": "Arepa de choclo con queso", "price_cop": 5000, "photo": 0},
        {"id": "arepa-de-carne-desmechada", "name": "Arepa de carne desmechada", "price_cop": 5000, "photo": 1},
    ],
    "hours": {"days": ["mon", "tue", "wed", "thu", "fri", "sat"], "open": "06:00", "close": "11:00"},
    "fulfillment": "pickup",
    "payment_methods": ["cash", "link"],
}

EXPECTED = {
    "number.profile": [],
    "brand.crops": [],
    "brand.logo": ["brand.crops"],
    "brand.hero": ["brand.crops"],
    "number.photo": ["brand.logo"],
    "menu.catalog": ["brand.hero"],
    "menu.flow": [],
    "agent.config": ["menu.flow"],
    "payments.ready": [],
    "inspector.run": ["agent.config", "payments.ready", "number.profile"],
    "launch.live": ["inspector.run"],
}


def rosa_spec() -> BusinessSpec:
    data = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else ROSA
    return BusinessSpec.model_validate(data)


def png(color: tuple[int, int, int] = (230, 180, 60), size: int = 64) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


class World:
    """In-memory stand-ins for whatsapp, muse, flowgen, store, agent, payments and inspector."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.profiles: dict[str, dict] = {"cradle-1": {"about": "Perfil viejo", "description": "", "address": "", "vertical": "OTHER"}}
        self.flows: dict[str, str] = {}
        self.businesses: dict[str, dict] = {}
        self.inspector_passes = True
        self.flow_errors: list[dict] = []
        self.modules = {name: types.ModuleType(f"genesis.{name}") for name in
                        ("whatsapp", "muse", "flowgen", "store", "agent", "payments", "inspector")}
        self._wire()

    def rec(self, name: str, *a, **kw) -> None:
        self.calls.append((name, a, kw))

    def ops(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, kw) for n, a, kw in self.calls if n == name]

    def _wire(self) -> None:
        w = self
        wa, muse, flowgen, store, agent, payments, inspector = (self.modules[k] for k in
                                                                ("whatsapp", "muse", "flowgen", "store", "agent", "payments", "inspector"))

        def get_business_profile(phone_id):
            w.rec("get_business_profile", phone_id)
            return dict(w.profiles.get(phone_id, {}))

        def update_business_profile(phone_id, **kw):
            w.rec("update_business_profile", phone_id, **kw)
            w.profiles.setdefault(phone_id, {}).update({k: v for k, v in kw.items() if v is not None})

        def create_flow(waba_id, name, categories):
            w.rec("create_flow", waba_id, name, categories)
            fid = f"flow-{len(w.flows) + 1}"
            w.flows[fid] = "DRAFT"
            return fid

        def update_flow_json(flow_id, flow_json):
            w.rec("update_flow_json", flow_id)
            return list(w.flow_errors)

        def publish_flow(flow_id):
            w.rec("publish_flow", flow_id)
            w.flows[flow_id] = "PUBLISHED"

        def deprecate_or_delete_flow(flow_id):
            w.rec("deprecate_or_delete_flow", flow_id)
            w.flows[flow_id] = "DEPRECATED" if w.flows.get(flow_id) == "PUBLISHED" else "DELETED"

        def upload_profile_picture(app_id, data, mime):
            w.rec("upload_profile_picture", app_id, len(data), mime)
            return "handle-1"

        def create_product(catalog_id, retailer_id, name, description, price_cop, image_url, url):
            w.rec("create_product", catalog_id, retailer_id, name, price_cop, image_url, url)
            return f"prod-{retailer_id}"

        def delete_product(catalog_id, retailer_id):
            w.rec("delete_product", catalog_id, retailer_id)

        def send_text(phone_id, to, body):
            w.rec("send_text", phone_id, to, body)
            return "wamid.1"

        def wa_me_link(display_number, text=""):
            return f"https://wa.me/{''.join(c for c in display_number if c.isdigit())}"

        for f in (get_business_profile, update_business_profile, create_flow, update_flow_json, publish_flow,
                  deprecate_or_delete_flow, upload_profile_picture, create_product, delete_product, send_text, wa_me_link):
            setattr(wa, f.__name__, f)

        def segment(image, prompt):
            w.rec("segment", prompt)
            return [{"box": [4, 4, 40, 40], "score": 0.91}, {"box": [0, 0, 10, 10], "score": 0.3}]

        muse.segment = segment
        muse.crop = lambda image, box, pad=0.06: png((200, 150, 40), 36)
        muse.image_generate = lambda prompt, size="1:1": (w.rec("image_generate", prompt), png((20, 120, 60), 128))[1]
        muse.image_edit = lambda image, prompt: (w.rec("image_edit", prompt), png((250, 240, 220), 96))[1]

        flowgen.order_flow = lambda spec: {"version": "7.0", "screens": [{"id": "ORDER"}, {"id": "CONFIRM", "terminal": True}]}
        flowgen.lint = lambda flow_json: []

        def save_business(cradle_phone_id, owner_wa_id, spec, flow_id="", system_prompt="", run_id=""):
            w.rec("save_business", cradle_phone_id, owner_wa_id, flow_id)
            live = w.businesses.get(cradle_phone_id, {}).get("live", False)
            w.businesses[cradle_phone_id] = {"spec": spec, "flow_id": flow_id, "system_prompt": system_prompt,
                                             "owner_wa_id": owner_wa_id, "live": live, "run_id": run_id}

        def set_live(cradle_phone_id, live):
            w.rec("set_live", cradle_phone_id, live)
            if cradle_phone_id in w.businesses:
                w.businesses[cradle_phone_id]["live"] = live

        def delete_business(cradle_phone_id):
            w.rec("delete_business", cradle_phone_id)
            w.businesses.pop(cradle_phone_id, None)

        store.save_business, store.set_live, store.delete_business = save_business, set_live, delete_business
        store.get_business = lambda cradle_phone_id: (dict(w.businesses[cradle_phone_id]) if cradle_phone_id in w.businesses else None)

        agent.system_prompt = lambda spec: f"Eres el asistente de IA de {spec.business_name}."
        agent.TOOLS = [{"type": "function", "function": {"name": n}} for n in ("consultar_menu", "crear_pedido", "link_de_pago")]

        payments.check = lambda: (w.rec("payments.check"), {"provider": "mock", "ok": True})[1]

        def inspect(cradle_phone_id, ctx_emit=None):
            w.rec("inspect", cradle_phone_id)
            ok = w.inspector_passes
            return {"passed": ok, "checks": [{"name": "precio", "ok": True, "detail": "$5.000"},
                                             {"name": "cerveza", "ok": ok, "detail": "se negó" if ok else "ofreció cerveza"}]}

        inspector.inspect = inspect

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, module in self.modules.items():
            monkeypatch.setitem(sys.modules, f"genesis.{name}", module)
            monkeypatch.setattr(genesis, name, module, raising=False)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    monkeypatch.setenv("GENESIS_MOCK", "1")
    monkeypatch.setenv("WA_DRY_RUN", "1")
    monkeypatch.setenv("GENESIS_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, catalog_id="cat-1", intake_phone_id="intake-1", waba_id="waba-1",
        cradle_phone_ids=["cradle-1"], cradle_display_numbers=["+57 300 123 4567"]))
    monkeypatch.setattr(plan.time, "sleep", lambda s: None)  # no backoff waits in tests
    w = World()
    w.install(monkeypatch)
    return w


_runs = iter(range(1, 10_000))


def make_ctx(tag: str) -> plan.Ctx:
    ctx = plan.Ctx(run_id=f"test-{tag}-{next(_runs)}", spec=rosa_spec(), photos=[png(), png((180, 90, 30))],
                   cradle_phone_id="cradle-1")
    ctx.owner_wa_id = "573001112233"  # dynamic attribute until plan.Ctx has the field
    return ctx


def events(ctx: plan.Ctx, kind: str) -> list:
    return [e for e in bus.log(ctx.run_id) if e.kind == kind]


def test_build_wires_the_contract_graph():
    ctx = make_ctx("build")
    nodes = blueprint.build(ctx)
    assert {n.id: n.deps for n in nodes} == EXPECTED
    non_critical = {n.id for n in nodes if not n.critical}
    assert non_critical == {"brand.crops", "brand.logo", "brand.hero", "number.photo", "menu.catalog"}
    assert all(n.revert for n in nodes if n.id in {"number.profile", "menu.flow", "menu.catalog", "agent.config", "launch.live"})


def test_happy_path_all_nodes_done(world: World, tmp_path: Path):
    ctx = make_ctx("happy")
    state = plan.State(str(tmp_path / "state.db"))
    out = plan.apply_plan(ctx, blueprint.build(ctx), state)

    assert set(out) == set(EXPECTED)
    assert not events(ctx, "node.failed") and not events(ctx, "node.reverted")
    # profile
    prof = world.profiles["cradle-1"]
    assert prof["about"] == (ctx.spec.about or prof["about"]) and 0 < len(prof["about"]) <= 139
    assert prof["vertical"] == "RESTAURANT"
    assert out["number.profile"]["previous"]["about"] == "Perfil viejo"
    assert prof["profile_picture_handle"] == "handle-1"
    # brand media on disk, served under /media/<run>/
    media = tmp_path / "media" / ctx.run_id
    assert out["brand.logo"]["path"] == f"/media/{ctx.run_id}/logo.png" and (media / "logo.png").is_file()
    assert (media / "hero.png").is_file() and (media / "crop_0.png").is_file() and (media / "crop_1.png").is_file()
    assert out["brand.crops"]["crops"][0]["box"] == [4, 4, 40, 40]
    # menu
    assert world.flows[out["menu.flow"]["flow_id"]] == "PUBLISHED"
    assert (media / "order_flow.json").is_file() and out["menu.flow"]["bytes"] > 0
    products = world.ops("create_product")
    assert [a[1] for a, _ in products] == ["arepas-rosa-arepa-de-choclo-con-queso", "arepas-rosa-arepa-de-carne-desmechada"]
    base = config.settings.public_base_url.rstrip("/")
    assert products[0][0][4] == f"{base}/media/{ctx.run_id}/crop_0.png"
    # agent + launch
    biz = world.businesses["cradle-1"]
    assert biz["live"] is True and biz["owner_wa_id"] == "573001112233" and biz["flow_id"] == out["menu.flow"]["flow_id"]
    assert out["launch.live"]["wa_me"] == "https://wa.me/573001234567"
    (phone, to, body), _ = world.ops("send_text")[0]
    assert phone == "intake-1" and to == "573001112233" and "tu negocio está vivo" in body.lower()
    assert "https://wa.me/573001234567" in body
    # panel details
    details = {(e.node, tuple(sorted(e.data))) for e in events(ctx, "node.detail")}
    nodes_with_detail = {n for n, _ in details}
    assert {"brand.crops", "brand.logo", "menu.flow", "inspector.run", "launch.live"} <= nodes_with_detail
    crop_detail = next(e for e in events(ctx, "node.detail") if e.node == "brand.crops" and "boxes" in e.data)
    assert crop_detail.data["boxes"][0]["box"] == [4, 4, 40, 40]


def test_inspector_failure_reverts_flow_and_profile(world: World, tmp_path: Path):
    world.inspector_passes = False
    ctx = make_ctx("fail")
    state = plan.State(str(tmp_path / "state.db"))
    with pytest.raises(plan.PlanError, match="inspector"):
        plan.apply_plan(ctx, blueprint.build(ctx), state)

    reverted = {e.node for e in events(ctx, "node.reverted")}
    assert {"menu.flow", "number.profile", "agent.config"} <= reverted
    assert list(world.flows.values()) == ["DEPRECATED"]
    assert world.profiles["cradle-1"]["about"] == "Perfil viejo"
    assert "cradle-1" not in world.businesses  # agent.config reverted
    assert not world.ops("send_text")  # Rosa never hears "vivo"
    assert not any(a[1] is True for a, _ in world.ops("set_live"))
    assert events(ctx, "run.failed")
    # reverted nodes are dropped from state, so a fixed retry re-applies them
    assert state.get(blueprint.build(ctx)[0].key(ctx)) is None


def test_second_run_is_idempotent(world: World, tmp_path: Path):
    state = plan.State(str(tmp_path / "state.db"))
    first = make_ctx("idem")
    out1 = plan.apply_plan(first, blueprint.build(first), state)
    calls_after_first = len(world.calls)

    second = make_ctx("idem")
    out2 = plan.apply_plan(second, blueprint.build(second), state)

    done = events(second, "node.done")
    assert len(done) == len(EXPECTED) and all(e.data.get("cached") for e in done)
    assert not events(second, "node.started")
    mutating = {"update_business_profile", "create_flow", "publish_flow", "create_product", "send_text",
                "save_business", "image_generate", "image_edit", "segment", "inspect", "upload_profile_picture"}
    assert not [c for c in world.calls[calls_after_first:] if c[0] in mutating]
    assert len(world.ops("send_text")) == 1
    assert out2["menu.flow"]["flow_id"] == out1["menu.flow"]["flow_id"]


def test_flow_validation_errors_fail_the_run_and_clean_up(world: World, tmp_path: Path):
    world.flow_errors = [{"error": "INVALID_PROPERTY", "message": "Property 'x' is not allowed", "line_start": 3}]
    ctx = make_ctx("flowerr")
    with pytest.raises(plan.PlanError, match="validation_errors"):
        plan.apply_plan(ctx, blueprint.build(ctx), plan.State(str(tmp_path / "state.db")))
    assert world.flows and all(s == "DELETED" for s in world.flows.values())
    errs = [e for e in events(ctx, "node.detail") if e.node == "menu.flow" and e.data.get("validation_errors")]
    assert errs and errs[0].data["validation_errors"][0]["error"] == "INVALID_PROPERTY"
    assert world.profiles["cradle-1"]["about"] == "Perfil viejo"


def test_brand_failure_is_not_fatal(world: World, tmp_path: Path):
    def boom(*a, **kw):
        raise RuntimeError("SAM caído")

    world.modules["muse"].segment = boom
    ctx = make_ctx("brandfail")
    out = plan.apply_plan(ctx, blueprint.build(ctx), plan.State(str(tmp_path / "state.db")))
    assert "launch.live" in out and world.businesses["cradle-1"]["live"] is True
    assert "brand.crops" not in out and "number.photo" not in out
    assert {e.node for e in events(ctx, "node.skipped")} >= {"brand.logo", "brand.hero", "number.photo", "menu.catalog"}


def test_no_catalog_and_no_photos(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, catalog_id=""))
    ctx = make_ctx("nocat")
    ctx.photos = []
    out = plan.apply_plan(ctx, blueprint.build(ctx), plan.State(str(tmp_path / "state.db")))
    assert out["menu.catalog"] == {"skipped": "no catalog"}
    assert out["brand.hero"] == {"skipped": "sin fotos"}
    assert out["brand.logo"]["path"].endswith("logo.png")  # logo comes from the spec alone
    assert not world.ops("create_product")


def test_with_real_sibling_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Same plan against the real modules that already exist (whatsapp dry-run, flowgen, store, payments
    mock, muse mock); stubs only for the ones still missing."""
    import importlib

    monkeypatch.setenv("GENESIS_MOCK", "1")
    monkeypatch.setenv("WA_DRY_RUN", "1")
    monkeypatch.setenv("GENESIS_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(plan.time, "sleep", lambda s: None)
    stubs = World()
    real = {}
    for name in ("whatsapp", "flowgen", "store", "payments", "muse", "agent", "inspector"):
        try:
            real[name] = importlib.import_module(f"genesis.{name}")
        except Exception:  # noqa: BLE001 - module not written yet
            monkeypatch.setitem(sys.modules, f"genesis.{name}", stubs.modules[name])
    if not {"whatsapp", "flowgen", "store"} <= set(real):
        pytest.skip("whatsapp/flowgen/store not available yet")
    real["store"].reset(str(tmp_path / "store.db"))
    real["whatsapp"].reset()
    try:
        ctx = make_ctx("real")
        ctx.cradle_display_number = "+57 300 123 4567"
        out = plan.apply_plan(ctx, blueprint.build(ctx), plan.State(str(tmp_path / "state.db")))
        assert {"number.profile", "menu.flow", "agent.config", "payments.ready", "inspector.run", "launch.live"} <= set(out)
        assert real["store"].get_business("cradle-1")["live"] is True
        assert real["whatsapp"].sent("publish_flow")
        assert any("vivo" in r["payload"]["text"]["body"].lower() for r in real["whatsapp"].sent("text"))
        assert out["launch.live"]["wa_me"].startswith("https://wa.me/573001234567")
    finally:
        real["store"].reset(None)


def test_owner_notification_failure_does_not_revert_launch(world: World, tmp_path: Path):
    def send_text(phone_id, to, body):
        raise RuntimeError("Graph 400 #131030 Recipient phone number not in allowed list")

    world.modules["whatsapp"].send_text = send_text
    ctx = make_ctx("notifyfail")
    out = plan.apply_plan(ctx, blueprint.build(ctx), plan.State(str(tmp_path / "state.db")))
    assert out["launch.live"]["live"] is True and out["launch.live"]["notified"] == ""
    assert world.businesses["cradle-1"]["live"] is True
    assert not events(ctx, "node.reverted")
    assert any("No pude avisar" in e.message for e in events(ctx, "node.detail") if e.node == "launch.live")


def test_publish_refused_launches_without_flow(world: World, tmp_path: Path):
    def publish_flow(flow_id):
        raise RuntimeError("You need to upload and sign a public key to a phone number before you can send or publish a flow")

    world.modules["whatsapp"].publish_flow = publish_flow
    ctx = make_ctx("nopublish")
    out = plan.apply_plan(ctx, blueprint.build(ctx), plan.State(str(tmp_path / "state.db")))
    assert out["menu.flow"]["published"] is False and out["menu.flow"]["flow_id"] == ""
    assert world.businesses["cradle-1"]["flow_id"] == "" and world.businesses["cradle-1"]["live"] is True
    assert world.flows and all(s == "DELETED" for s in world.flows.values())


def test_flow_names_are_unique_per_run():
    from genesis.lanes import menu

    a, b = make_ctx("name"), make_ctx("name")
    assert menu.flow_name(a) != menu.flow_name(b)
    assert len(menu.flow_name(a)) <= 100 and a.spec.business_name in menu.flow_name(a)

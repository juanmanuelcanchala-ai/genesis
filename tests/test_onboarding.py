from __future__ import annotations

import dataclasses
import time

import pytest

from genesis import onboarding
from genesis.events import bus
from tests.f_fakes import Inbound, install, rosa_spec

INTAKE = "intake"


@pytest.fixture
def fk(monkeypatch, tmp_path):
    f = install(monkeypatch)
    monkeypatch.setattr(onboarding, "BACKGROUND", False)
    monkeypatch.setattr(onboarding, "DB_PATH", str(tmp_path / "intake.db"))
    monkeypatch.setattr(onboarding, "AUTO_COMPILE_S", 0)  # timer disabled unless a test enables it
    onboarding.reset()
    yield f
    onboarding.reset()


def kinds(run_id: str) -> list[str]:
    return [e.kind for e in bus.log(run_id)]


def msg(wa_id: str, **kw) -> Inbound:
    return Inbound(phone_id=INTAKE, wa_id=wa_id, **kw)


def test_start_run_auto_confirm_goes_live(fk):
    run_id = onboarding.start_run("573111", b"OGG", "audio/ogg", [b"p1", b"p2"])
    run = onboarding.get_run(run_id)
    assert run["state"] == "live"
    assert run["wa_me"].startswith("https://wa.me/")
    ks = kinds(run_id)
    for k in ("run.started", "spec.ready", "plan.ready", "node.done", "run.live"):
        assert k in ks
    assert ks.index("spec.ready") < ks.index("plan.ready") < ks.index("run.live")
    spec_ev = next(e for e in bus.log(run_id) if e.kind == "spec.ready")
    assert spec_ev.data["transcript"]["segments"][0]["start"] == 0.0
    assert spec_ev.data["spec"]["business_name"] == "Arepas Rosa"
    hl = spec_ev.data["highlights"]
    assert [h["source_s"] for h in hl if h["source_s"] is not None] == sorted(h["source_s"] for h in hl if h["source_s"] is not None)
    live = next(e for e in bus.log(run_id) if e.kind == "run.live")
    assert live.data["business_name"] == "Arepas Rosa"
    assert live.data["inspector"]["passed"] is True
    ctx = fk.built[0]
    assert ctx.cradle_phone_id == "cradle-1"
    assert ctx.out("intake")["owner_wa_id"] == "573111"
    assert ctx.owner_wa_id == "573111"
    assert ctx.photos == [b"p1", b"p2"]
    # API runs do not chat with the seller from the intake number
    assert fk.sent == []
    # mirrored in SQLite
    onboarding.RUNS.clear()
    assert onboarding.get_run(run_id)["state"] == "live"


def test_whatsapp_flow_question_confirm_live(fk):
    fk.question = "¿Hasta qué hora atiendes?"
    wa = "573222"
    onboarding.handle_intake(msg(wa, text="hola"))
    assert "nota de voz" in fk.sent[-1]["body"]
    onboarding.handle_intake(msg(wa, type="audio", media_id="aud1"))
    onboarding.handle_intake(msg(wa, type="image", media_id="img1"))
    onboarding.handle_intake(msg(wa, type="image", media_id="img2"))
    onboarding.handle_intake(msg(wa, text="Listo"))
    sess = onboarding.SESSIONS[wa]
    run = onboarding.RUNS[sess.run_id]
    assert fk.compile_calls[0]["photos"] == 2 and fk.compile_calls[0]["audio"] == b"OGG"
    assert run.state == "asking"
    assert fk.sent[-1]["body"] == fk.question
    # the answer can be a voice note: it is transcribed
    onboarding.handle_intake(msg(wa, type="audio", media_id="aud2"))
    assert fk.compile_calls[-1]["answer"] == "hasta las once"
    assert fk.compile_calls[-1]["previous"] is not None
    assert run.state == "confirming"
    assert fk.sent[-1]["type"] == "buttons"
    assert "sí" in fk.sent[-1]["body"]
    onboarding.handle_intake(msg(wa, text="Sí"))
    assert run.state == "live"
    assert "run.live" in kinds(run.run_id)
    # only one question was ever asked
    assert sum(1 for s in fk.sent if s["body"] == fk.question) == 1
    # a later text gets the live link
    onboarding.handle_intake(msg(wa, text="hola"))
    assert run.wa_me in fk.sent[-1]["body"]


def test_confirm_button_and_correction(fk):
    wa = "573333"
    onboarding.handle_intake(msg(wa, type="audio", media_id="aud1"))
    onboarding.handle_intake(msg(wa, type="image", media_id="img1"))
    run_id = onboarding.flush(wa)
    run = onboarding.RUNS[run_id]
    assert run.state == "confirming"  # no question needed
    onboarding.handle_intake(msg(wa, text="la de carne vale 6000"))
    assert fk.compile_calls[-1]["answer"] == "la de carne vale 6000"
    assert run.state == "confirming"
    onboarding.handle_intake(msg(wa, type="interactive", button_id=onboarding.BTN_YES))
    assert run.state == "live"


def test_listo_without_audio_asks_for_voice_note(fk):
    onboarding.handle_intake(msg("573444", type="image", media_id="img1"))
    onboarding.handle_intake(msg("573444", text="listo"))
    assert "nota de voz" in fk.sent[-1]["body"]
    assert onboarding.SESSIONS["573444"].run_id == ""


def test_max_three_photos(fk):
    wa = "573555"
    onboarding.handle_intake(msg(wa, type="audio", media_id="aud1"))
    for i in range(4):
        onboarding.handle_intake(msg(wa, type="image", media_id=f"img{i}"))
    assert len(onboarding.SESSIONS[wa].photos) == 3
    assert "3 fotos" in fk.sent[-1]["body"]


def test_timer_flushes_after_last_photo(fk, monkeypatch):
    monkeypatch.setattr(onboarding, "AUTO_COMPILE_S", 0.05)
    wa = "573666"
    onboarding.handle_intake(msg(wa, type="audio", media_id="aud1"))
    assert onboarding.SESSIONS[wa].timer is None  # no photo yet: no timer
    onboarding.handle_intake(msg(wa, type="image", media_id="img1"))
    deadline = time.time() + 3
    while time.time() < deadline and not onboarding.SESSIONS[wa].run_id:
        time.sleep(0.02)
    run_id = onboarding.SESSIONS[wa].run_id
    assert run_id
    assert onboarding.RUNS[run_id].state == "confirming"


def test_blocked_category(fk):
    fk.blocked = "alcohol"
    wa = "573777"
    onboarding.handle_intake(msg(wa, type="audio", media_id="aud1"))
    run_id = onboarding.flush(wa)
    run = onboarding.RUNS[run_id]
    assert run.state == "blocked"
    failed = [e for e in bus.log(run_id) if e.kind == "run.failed"]
    assert failed and failed[0].data["blocked"] == "alcohol"
    assert "bebidas alcohólicas" in fk.sent[-1]["body"]
    assert fk.built == []


def test_plan_failure_reverts_and_notifies(fk):
    fk.fail_node = True
    wa = "573888"
    onboarding.handle_intake(msg(wa, type="audio", media_id="aud1"))
    run_id = onboarding.flush(wa)
    onboarding.handle_intake(msg(wa, text="dale"))
    run = onboarding.RUNS[run_id]
    assert run.state == "failed"
    assert kinds(run_id).count("run.failed") == 1
    assert "deshice" in fk.sent[-1]["body"]


def test_assign_cradle_prefers_free_then_own(fk, monkeypatch):
    s = dataclasses.replace(onboarding.settings, cradle_phone_ids=["c1", "c2"], cradle_display_numbers=["5731", "5732"])
    monkeypatch.setattr(onboarding, "settings", s)
    fk.businesses["c1"] = {"owner_wa_id": "someone"}
    assert onboarding.assign_cradle("me") == ("c2", "5732")
    fk.businesses["c2"] = {"owner_wa_id": "me"}
    assert onboarding.assign_cradle("me") == ("c2", "5732")
    with pytest.raises(onboarding.NoCradle):
        onboarding.assign_cradle("other")


def test_mock_cradle_when_none_configured(fk):
    assert onboarding.assign_cradle("x")[0] == "cradle-1"


def test_is_done_and_highlights():
    assert onboarding.is_done("Listo!")
    assert onboarding.is_done("ya terminé")
    assert not onboarding.is_done("hola")
    hl = onboarding.highlights(rosa_spec())
    fields = [h["field"] for h in hl]
    assert fields[0] == "business_name"
    assert "products.choclo" in fields and "hours" in fields
    hours = next(h for h in hl if h["field"] == "hours")
    assert hours["value"] == "lun–sáb 06:00–11:00"
    assert any(h["value"].endswith("$5.000") for h in hl)


def test_redelivered_intake_photo_counted_once(fk, monkeypatch):
    import sys

    seen: set[str] = set()
    monkeypatch.setattr(sys.modules["genesis.store"], "mark_seen",
                        lambda mid: not (mid in seen or seen.add(mid)), raising=False)
    wa = "573222"
    onboarding.handle_intake(msg(wa, type="image", media_id="img1", message_id="wamid.P1"))
    onboarding.handle_intake(msg(wa, type="image", media_id="img1", message_id="wamid.P1"))  # Meta retry
    onboarding.handle_intake(msg(wa, type="image", media_id="img2", message_id="wamid.P2"))
    assert len(onboarding._session(wa).photos) == 2


def test_confirm_only_once_under_race(fk, monkeypatch):
    import threading

    wa = "573333"
    onboarding.handle_intake(msg(wa, type="audio", media_id="a1", message_id="wamid.A"))
    run_id = onboarding.flush(wa)
    assert onboarding.RUNS[run_id].state == "confirming"
    builds: list[str] = []
    monkeypatch.setattr(onboarding, "_build", lambda run: builds.append(run.run_id))
    results: list[bool] = []
    ts = [threading.Thread(target=lambda: results.append(onboarding.confirm(run_id))) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(True) == 1 and builds == [run_id]


def test_mock_cradle_busy_raises(fk):
    run = onboarding.Run(run_id="busy-1", owner_wa_id="x", audio=b"", mime="audio/ogg", photos=[])
    run.state, run.cradle_phone_id = "building", onboarding.MOCK_CRADLE
    onboarding.RUNS[run.run_id] = run
    with pytest.raises(onboarding.NoCradle):
        onboarding.assign_cradle("y")

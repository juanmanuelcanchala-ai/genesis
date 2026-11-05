"""GET /api/runs/{id}/export turns a live run (bus events + inbound/outbound logs) into run.json for the video."""

import time
from dataclasses import replace

from fastapi.testclient import TestClient

from genesis import server, whatsapp
from genesis.events import Event, bus


def test_export_live_run(monkeypatch):
    rid = "exp-1"
    t0 = time.time()
    bus.emit(Event(rid, "run.started", message="Nota de voz recibida"))
    bus.emit(Event(rid, "intake.asking", message="¿Recogen o domicilio?"))
    intake = "intake-1"
    monkeypatch.setattr(server, "settings", replace(server.settings, intake_phone_id=intake, demo_owner_wa_id="573101234567"))
    monkeypatch.setattr(server, "INBOUND_LOG", [{"t": t0 + 0.1, "phone_id": intake, "wa_id": "573101234567", "name": "R",
                                                 "message_id": "m1", "type": "audio", "text": "", "button_id": "",
                                                 "flow_response": None}])
    whatsapp.LIVE_LOG.append({"t": t0 + 0.2, "op": "text", "id": "wamid.x",
                              "payload": {"phone_id": intake, "to": "573101234567", "type": "text", "text": {"body": "¡Recibí tu nota de voz!"}}})
    r = TestClient(server.app).get(f"/api/runs/{rid}/export")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["live_whatsapp"] and d["question"] == "¿Recogen o domicilio?"
    whos = [ln["who"] for ln in d["lines"]]
    assert "Rosa → Génesis" in whos and "Génesis → Rosa" in whos
    assert any(ln["text"] == "🎤 nota de voz" for ln in d["lines"])
    assert TestClient(server.app).get("/api/runs/nope/export").status_code == 404

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from genesis import onboarding, server
from genesis.events import Event, bus
from tests.f_fakes import install


@pytest.fixture
def fk(monkeypatch, tmp_path):
    f = install(monkeypatch)
    monkeypatch.setattr(onboarding, "BACKGROUND", False)
    monkeypatch.setattr(onboarding, "DB_PATH", str(tmp_path / "intake.db"))
    monkeypatch.setattr(onboarding, "AUTO_COMPILE_S", 0)
    monkeypatch.setattr(server, "BACKGROUND", False)
    monkeypatch.setattr(server, "MEDIA_DIR", tmp_path / "media")
    onboarding.reset()
    yield f
    onboarding.reset()


@pytest.fixture
def client(fk):
    return TestClient(server.app)


def test_webhook_verify(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "genesis-verify", "hub.challenge": "123"})
    assert ok.status_code == 200 and ok.text == "123"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403


def test_webhook_bad_signature(client):
    r = client.post("/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 403


def test_webhook_routes_intake_and_cradle(client, fk, monkeypatch):
    seen = []
    monkeypatch.setattr(onboarding, "handle_intake", lambda inbound: seen.append(inbound.wa_id))
    fk.businesses["cradle-1"] = {"run_id": "r-1", "owner_wa_id": "rosa"}
    payload = {"fake": [
        {"phone_id": "intake", "wa_id": "seller1", "type": "text", "text": "hola"},
        {"phone_id": "cradle-1", "wa_id": "cust1", "type": "text", "text": "¿a cómo la arepa?"},
    ]}
    r = client.post("/webhook", content=json.dumps(payload), headers={"X-Hub-Signature-256": "sha256=ok"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert seen == ["seller1"]
    assert [c[0] for c in fk.agent_calls] == ["cradle-1"]
    cust = [e for e in bus.log("r-1") if e.kind == "customer.msg"]
    assert cust and "arepa" in cust[-1].message


def test_demo_run_end_to_end_and_sse(client):
    r = client.post("/api/runs?demo=1&auto_confirm=1")
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    info = client.get(f"/api/runs/{run_id}").json()
    assert info["state"] == "live" and info["business_name"] == "Arepas Rosa"
    assert info["events"] >= 5
    with client.stream("GET", f"/api/runs/{run_id}/events?close=1") as s:
        body = "".join(s.iter_text())
    assert "event: run.started" in body and "event: spec.ready" in body and "event: run.live" in body
    with client.stream("GET", f"/api/runs/{run_id}/events?close=1&plain=1") as s:
        body = "".join(s.iter_text())
    assert "event:" not in body
    datas = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert datas[-1]["kind"] == "run.live" and datas[-1]["data"]["wa_me"].startswith("https://wa.me/")
    assert client.get("/api/runs").json()[0]["run_id"] == run_id


def test_api_run_multipart(client, fk, monkeypatch):
    calls = []

    def fake_start(owner, audio, mime, photos, answer="", *, auto_confirm=True, via="api"):
        calls.append((owner, audio, mime, len(photos), answer, auto_confirm))
        return "r-x"

    monkeypatch.setattr(onboarding, "start_run", fake_start)
    files = [
        ("audio", ("nota.ogg", b"OGGDATA", "audio/ogg")),
        ("photos[]", ("a.jpg", b"JPG1", "image/jpeg")),
        ("photos", ("b.jpg", b"JPG2", "image/jpeg")),
        ("photos[]", ("x.txt", b"nope", "text/plain")),
    ]
    r = client.post("/api/runs?auto_confirm=0", files=files, data={"owner": "57300", "answer": "hasta las 11"})
    assert r.status_code == 201
    assert calls == [("57300", b"OGGDATA", "audio/ogg", 2, "hasta las 11", False)]


def test_api_run_requires_audio(client):
    assert client.post("/api/runs").status_code == 422


def test_confirm_endpoint(client, fk):
    wa = "573999"
    from tests.f_fakes import Inbound

    onboarding.handle_intake(Inbound(phone_id="intake", wa_id=wa, type="audio", media_id="a1"))
    run_id = onboarding.flush(wa)
    assert client.post(f"/api/runs/{run_id}/confirm").status_code == 200
    assert onboarding.RUNS[run_id].state == "live"
    assert client.post(f"/api/runs/{run_id}/confirm").status_code == 409


def test_mock_pay_page_and_post(client, fk):
    fk.businesses["cradle-1"] = {"run_id": "r-pay"}
    page = client.get("/pay/mock/%231")
    assert page.status_code == 200 and "Pagar (simulado)" in page.text
    done = client.post("/pay/mock/%231")
    assert done.status_code == 200 and "Pago aprobado" in done.text
    assert fk.paid == ["#1"]
    paid = [e for e in bus.log("r-pay") if e.kind == "order.paid"]
    assert paid and "$10.000" in paid[-1].message


def test_mp_webhook(client, fk):
    r = client.post("/mp/webhook?type=payment&data.id=42", json={"action": "payment.updated", "data": {"id": "42"}})
    assert r.status_code == 200
    assert fk.mp_calls[0][1]["data.id"] == "42"


def test_media_and_traversal(client, tmp_path):
    d = tmp_path / "media" / "run1"
    d.mkdir(parents=True)
    (d / "logo.png").write_bytes(b"\x89PNG")
    (tmp_path / "secret.txt").write_text("x")
    assert client.get("/media/run1/logo.png").content == b"\x89PNG"
    assert client.get("/media/../secret.txt").status_code == 404
    assert client.get("/media/run1/%2e%2e/%2e%2e/secret.txt").status_code == 404


def test_businesses(client, fk):
    from tests.f_fakes import rosa_spec

    fk.businesses["cradle-1"] = {"spec": rosa_spec(), "live": True, "run_id": "r1", "flow_id": "f1", "owner_wa_id": "x"}
    rows = client.get("/api/businesses").json()
    assert rows[0]["business_name"] == "Arepas Rosa" and rows[0]["live"] is True and rows[0]["orders"] == 1


def test_panel_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Tu negocio" in r.text and "QR-BEGIN" in r.text and "/api/runs?demo=1" in r.text


def test_health(client):
    assert client.get("/health").json()["ok"] is True


@pytest.mark.skipif(not (shutil.which("node") and shutil.which("zbarimg")), reason="needs node and zbarimg")
def test_panel_qr_scans(tmp_path):
    """The inline QR encoder in the panel must produce codes a phone can scan (checked with zbar)."""
    html = (Path(server.PANEL)).read_text()
    code = re.search(r"/\* QR-BEGIN.*?/\* QR-END \*/", html, re.S).group(0)
    url = "https://wa.me/573000000001?text=Hola%20Arepas%20Rosa%2C%20quiero%20hacer%20un%20pedido"
    js = code + f"""
const m = QR.encode({json.dumps(url)}), b = 4, s = 5, n = (m.length + 2 * b) * s;
let out = `P1\\n${{n}} ${{n}}\\n`;
for (let y = 0; y < n; y++) {{ const r = []; for (let x = 0; x < n; x++) {{
  const yy = Math.floor(y / s) - b, xx = Math.floor(x / s) - b;
  r.push(yy >= 0 && xx >= 0 && yy < m.length && xx < m.length && m[yy][xx] ? 1 : 0); }}
  out += r.join(" ") + "\\n"; }}
require("fs").writeFileSync(process.argv[2], out);
"""
    script = tmp_path / "qr.js"
    script.write_text(js)
    pbm = tmp_path / "qr.pbm"
    subprocess.run(["node", str(script), str(pbm)], check=True, timeout=30)
    out = subprocess.run(["zbarimg", "-q", "--raw", str(pbm)], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == url


def test_sse_stream_all_plain(fk):
    # The generator is exercised directly: an infinite stream is awkward through TestClient.
    resp = server._sse("*", close_on_end=True, plain=True)
    gen = resp.body_iterator
    bus.emit(Event("r-z", "run.started", message="x"))
    bus.emit(Event("r-z", "run.live", message="y"))
    import asyncio

    async def collect():
        return [chunk async for chunk in gen]

    chunks = asyncio.run(collect())
    assert chunks[0].startswith("retry:")
    assert chunks[1].startswith("data: ") and '"run.started"' in chunks[1]


def _seen_store(monkeypatch):
    import sys

    seen: set[str] = set()

    def mark_seen(mid: str) -> bool:
        if mid in seen:
            return False
        seen.add(mid)
        return True

    monkeypatch.setattr(sys.modules["genesis.store"], "mark_seen", mark_seen, raising=False)


def test_admin_token_guards_stage_api(client, monkeypatch):
    monkeypatch.setenv("GENESIS_ADMIN_TOKEN", "s3cret")
    assert client.get("/api/runs").status_code == 401
    assert client.post("/api/runs?demo=1").status_code == 401
    assert client.post("/api/runs/x/confirm").status_code == 401
    assert client.get("/api/businesses", params={"token": "nope"}).status_code == 401
    assert client.get("/api/runs", headers={"X-Genesis-Token": "s3cret"}).status_code == 200
    assert client.get("/api/runs", params={"token": "s3cret"}).status_code == 200
    # public endpoints stay open
    assert client.get("/health").status_code == 200
    assert client.post("/mp/webhook", json={}).status_code == 200


def test_admin_api_closed_live_without_token(client, monkeypatch):
    import dataclasses

    monkeypatch.delenv("GENESIS_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("WA_DRY_RUN", "0")
    monkeypatch.setattr(server, "settings", dataclasses.replace(server.settings, wa_token="TOKEN"))
    assert client.get("/api/runs").status_code == 503


def test_redelivered_customer_message_shown_once(client, fk, monkeypatch):
    _seen_store(monkeypatch)
    fk.businesses["cradle-1"] = {"run_id": "r-dup", "owner_wa_id": "rosa"}
    payload = {"fake": [{"phone_id": "cradle-1", "wa_id": "c1", "message_id": "wamid.D", "text": "hola"}] * 2}
    client.post("/webhook", content=json.dumps(payload), headers={"X-Hub-Signature-256": "sha256=ok"})
    assert len([e for e in bus.log("r-dup") if e.kind == "customer.msg"]) == 1

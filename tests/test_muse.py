"""Tests for genesis.muse: mock branch (offline) and real-branch parsing with respx (no network)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from genesis import muse
from genesis.config import settings
from genesis.spec import BusinessSpec

BASE = settings.model_base_url.rstrip("/")


class FakeOpenAI:
    """Stand-in for openai.OpenAI (the SDK ships its own HTTP stack, which respx cannot intercept)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        content, tool_calls = r if isinstance(r, tuple) else (r, None)
        tcs = [SimpleNamespace(id=t["id"], function=SimpleNamespace(name=t["name"], arguments=t["arguments"])) for t in tool_calls or []]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tcs or None))])


def _fake(monkeypatch, *replies):
    fake = FakeOpenAI(replies)
    monkeypatch.setattr(muse, "_client", lambda: fake)
    return fake


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setenv("GENESIS_MOCK", "1")
    muse.set_mock_spec(None)
    yield
    muse.set_mock_spec(None)


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setenv("GENESIS_MOCK", "0")
    yield


# ------------------------------------------------------------------ fixtures


def test_rosa_spec_fixture_is_valid():
    spec = BusinessSpec.model_validate(muse.load_fixture("rosa_spec.json"))
    assert spec.business_name == "Arepas Rosa" and spec.owner_name == "Rosa"
    assert [p.price_cop for p in spec.products] == [5000, 5000]
    assert spec.hours and spec.hours.days == ["mon", "tue", "wed", "thu", "fri", "sat"]
    assert (spec.hours.open, spec.hours.close) == ("06:00", "11:00")
    assert spec.neighborhood == "San Fernando" and spec.fulfillment.value == "pickup"
    assert [m.value for m in spec.payment_methods] == ["cash", "link"]
    assert all(p.evidence.source_s is not None for p in spec.products)
    assert spec.evidence["business_name"].source_s == 22.4


def test_transcript_fixture_segments_cover_evidence():
    fx = muse.load_fixture("rosa_transcript.json")
    starts = {s["start"] for s in fx["segments"]}
    spec = muse.load_fixture("rosa_spec.json")
    for ev in spec["evidence"].values():
        assert ev["source_s"] in starts
    assert spec["hours"]["evidence"]["source_s"] in starts


# ------------------------------------------------------------------ mock branch


def test_transcribe_mock(mock_mode):
    t = muse.transcribe(b"OggS...", "audio/ogg")
    assert isinstance(t, muse.Transcript)
    assert "Arepas Rosa" in t.text and len(t.segments) == 6
    assert t.segments[0]["start"] == 0.0


def test_structured_mock_returns_rosa_spec(mock_mode):
    from genesis import spec as spec_mod

    out = muse.structured("nota de voz...", spec_mod.json_schema(), "business_spec", images=[muse.mock_png(8, 8, (0, 0, 0), (255, 255, 255))])
    assert BusinessSpec.model_validate(out).business_name == "Arepas Rosa"


def test_structured_mock_registry_and_generic(mock_mode, monkeypatch):
    monkeypatch.setitem(muse.MOCK_STRUCTURED, "custom", {"ok": True})
    assert muse.structured("x", {"type": "object"}, "custom") == {"ok": True}
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "n": {"type": "integer", "default": 3},
                                               "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["a", "n", "tags"]}
    assert muse.structured("x", schema, "generic") == {"a": "", "n": 3, "tags": []}


def _ask(text, tools=None):
    return muse.chat([{"role": "system", "content": "Eres el asistente de Arepas Rosa."}, {"role": "user", "content": text}], tools)


TOOLS = [{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}} for n in
         ("consultar_menu", "crear_pedido", "link_de_pago", "estado_pedido", "pasar_a_humano")]


def test_chat_mock_price(mock_mode):
    r = _ask("¿Cuánto vale la arepa de choclo?")
    assert "$5.000" in r["content"] and "choclo" in r["content"] and r["tool_calls"] == []
    assert "$5.000" in _ask("precios?")["content"]


def test_chat_mock_hours(mock_mode):
    r = _ask("¿A qué hora abren?")
    assert "lunes a sábado" in r["content"] and "6:00 a. m." in r["content"] and "11:00 a. m." in r["content"]


def test_chat_mock_pickup(mock_mode):
    r = _ask("¿Hacen domicilio?")
    assert "no hacemos domicilios" in r["content"] and "San Fernando" in r["content"]


def test_chat_mock_refuses_alcohol(mock_mode):
    r = _ask("¿Venden cerveza?", TOOLS)
    assert "no vendemos" in r["content"].lower() and r["tool_calls"] == []


def test_chat_mock_order_calls_tool(mock_mode):
    r = _ask("quiero 2 de arepa de carne desmechada", TOOLS)
    assert r["content"] is None and len(r["tool_calls"]) == 1
    call = r["tool_calls"][0]
    assert call["name"] == "crear_pedido" and call["id"]
    assert call["arguments"]["items"] == [{"product_id": "arepa-de-carne-desmechada", "name": "Arepa de carne desmechada", "qty": 2, "unit_cop": 5000}]
    assert call["arguments"]["pickup_time"] == "06:00"


def test_chat_mock_order_without_tools_offers_flow(mock_mode):
    assert "Hacer pedido" in _ask("quiero dos arepas")["content"]


def test_chat_mock_after_tool_result(mock_mode):
    msgs = [{"role": "user", "content": "quiero 2"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "crear_pedido", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"id": "#1", "total_cop": 10000})}]
    r = muse.chat(msgs, TOOLS)
    assert "#1" in r["content"] and "$10.000" in r["content"] and r["tool_calls"] == []


def test_chat_mock_out_of_scope_hands_off(mock_mode):
    r = _ask("¿Quién ganó el partido de anoche?")
    assert "Rosa" in r["content"] and "asistente con IA" in r["content"]


def test_chat_mock_reads_spec_from_system_prompt(mock_mode):
    spec = muse.load_fixture("rosa_spec.json")
    spec.update(business_name="Postres Ana", owner_name="Ana", category="postres", fulfillment="delivery", delivery_zone="Ciudad Jardín")
    spec["products"] = [{"id": "torta-de-tres-leches", "name": "Torta de tres leches", "price_cop": 8000}]
    sys = "Eres el asistente de Postres Ana.\nDATOS: " + json.dumps(spec, ensure_ascii=False)
    r = muse.chat([{"role": "system", "content": sys}, {"role": "user", "content": "cuánto vale la torta?"}])
    assert "$8.000" in r["content"]
    r = muse.chat([{"role": "system", "content": sys}, {"role": "user", "content": "hacen domicilio?"}])
    assert "Ciudad Jardín" in r["content"]


def test_segment_crop_and_images_mock(mock_mode):
    img = muse.mock_png(200, 100, (255, 255, 255), (200, 0, 0))
    assert muse.image_size(img) == (200, 100)
    boxes = muse.segment(img, "arepa")
    assert boxes and len(boxes[0]["box"]) == 4 and 0 <= boxes[0]["score"] <= 1
    c = muse.crop(img, boxes[0]["box"])
    w, h = muse.image_size(c)
    assert 0 < w < 200 and 0 < h <= 100
    logo = muse.image_generate("logo Arepas Rosa")
    assert muse.sniff_mime(logo) == "image/png" and muse.image_size(logo) == (256, 256)
    wide = muse.image_generate("banner", "16:9")
    assert muse.image_size(wide)[0] > muse.image_size(wide)[1]
    hero = muse.image_edit(c, "fondo limpio")
    assert muse.sniff_mime(hero) == "image/png"


def test_crop_clamps_and_degenerate_box():
    img = muse.mock_png(50, 50, (0, 0, 0), (255, 255, 255))
    assert muse.image_size(muse.crop(img, [-10, -10, 80, 80])) == (50, 50)
    assert muse.crop(img, [10, 10, 10, 10]) == img


# ------------------------------------------------------------------ real-branch parsing (respx, no network)


def test_parse_sam_text():
    text = ("<0f>0<|box;x1=10;y1=20;x2=110;y2=90;w=640;h=480;c=0.81|><|mask;x=0;y=0;c=0.81;data=2,3,~!!!|>,"
            "1<|box;x1=200;y1=50;x2=260;y2=120;w=640;h=480;c=0.95|><|mask;x=0;y=0;c=0.95;data=3,4,~!!|>")
    boxes = muse.parse_sam_text(text)
    assert boxes == [{"box": [200.0, 50.0, 260.0, 120.0], "score": 0.95}, {"box": [10.0, 20.0, 110.0, 90.0], "score": 0.81}]
    half = muse.parse_sam_text(text, (320, 240))
    assert half[0]["box"] == [100.0, 25.0, 130.0, 60.0]
    assert muse.parse_sam_text("") == []
    assert muse.parse_sam_text("<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10|>")[0]["score"] == 1.0


@respx.mock
def test_segment_live(live_mode):
    img = muse.mock_png(64, 48, (255, 255, 255), (200, 0, 0))
    route = respx.post(f"{BASE}/responses").mock(return_value=httpx.Response(200, json={
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "<0f>0<|box;x1=5;y1=6;x2=40;y2=30;w=64;h=48;c=0.9|><|mask;x=0;y=0;data=1,1,!x|>"}]}]}))
    assert muse.segment(img, "arepa") == [{"box": [5.0, 6.0, 40.0, 30.0], "score": 0.9}]
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == settings.model_sam
    parts = body["input"][0]["content"]
    assert parts[0] == {"type": "input_text", "text": "arepa"} and parts[1]["image_url"].startswith("data:image/png;base64,")


@respx.mock
def test_segment_live_no_match_returns_empty(live_mode):
    respx.post(f"{BASE}/responses").mock(return_value=httpx.Response(200, json={"output_text": ""}))
    assert muse.segment(muse.mock_png(8, 8, (0, 0, 0), (1, 1, 1)), "cerveza") == []


@respx.mock
def test_segment_live_falls_back_to_spark(live_mode, monkeypatch):
    respx.post(f"{BASE}/responses").mock(return_value=httpx.Response(500, json={"error": "x"}))
    _fake(monkeypatch, json.dumps({"boxes": [{"box": [1, 2, 30, 40], "score": 0.7}, {"box": [5, 5, 1, 1], "score": 0.9}]}))
    assert muse.segment(muse.mock_png(64, 64, (0, 0, 0), (1, 1, 1)), "arepa") == [{"box": [1.0, 2.0, 30.0, 40.0], "score": 0.7}]


@respx.mock
def test_transcribe_live(live_mode, monkeypatch):
    monkeypatch.setattr(muse, "to_wav16k", lambda audio, mime="audio/ogg": b"RIFFfakeWAVE")
    route = respx.post(f"{BASE}/asr/transcribe").mock(return_value=httpx.Response(200, json={
        "sessionId": "s1", "transcript": "Hola, soy Rosa. Vendo arepas.", "audioDurationMs": 5000,
        "turns": [{"turnId": 1, "startMs": 2500, "endMs": 5000, "transcript": "Vendo arepas."},
                  {"turnId": 0, "startMs": 0, "endMs": 2500, "transcript": "Hola, soy Rosa."}]}))
    t = muse.transcribe(b"OggS", "audio/ogg")
    assert t.text == "Hola, soy Rosa. Vendo arepas."
    assert t.segments == [{"start": 0.0, "end": 2.5, "text": "Hola, soy Rosa."}, {"start": 2.5, "end": 5.0, "text": "Vendo arepas."}]
    req = route.calls[0].request
    assert req.headers["authorization"].startswith("Bearer ")
    content = req.content.decode(errors="ignore")
    assert '"audioEncoding": "WAV"' in content and 'name="audio"' in content and settings.model_transcribe in content


@respx.mock
def test_transcribe_live_falls_back_to_chat_audio(live_mode, monkeypatch):
    monkeypatch.setattr(muse, "to_wav16k", lambda audio, mime="audio/ogg": b"RIFFfakeWAVE")
    respx.post(f"{BASE}/asr/transcribe").mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    fake = _fake(monkeypatch, "Hola, soy Rosa.")
    t = muse.transcribe(b"OggS")
    assert t.text == "Hola, soy Rosa." and t.segments == [] and t.model == settings.model_audio
    body = fake.calls[0]
    assert body["model"] == settings.model_audio
    assert body["messages"][0]["content"][1]["type"] == "input_audio"


def test_to_wav16k_converts_with_ffmpeg():
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    ogg = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                          "-c:a", "libopus", "-f", "ogg", "pipe:1"], capture_output=True).stdout
    if not ogg:
        pytest.skip("ffmpeg without libopus")
    wav = muse.to_wav16k(ogg, "audio/ogg")
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    import struct

    channels, rate = struct.unpack("<HI", wav[22:28])
    assert (channels, rate) == (1, 16000)


def test_structured_live_retries_invalid_json(live_mode, monkeypatch):
    fake = _fake(monkeypatch, "no es json", '```json\n{"business_name": "Arepas Rosa"}\n```')
    img = muse.mock_png(4, 4, (0, 0, 0), (1, 1, 1))
    out = muse.structured("p", {"type": "object"}, "business spec", system="sys", images=[img], reasoning="medium")
    assert out == {"business_name": "Arepas Rosa"}
    first = fake.calls[0]
    assert first["model"] == settings.model_spec and first["extra_body"] == {"reasoning_effort": "medium"}
    assert first["response_format"]["type"] == "json_schema"
    assert first["response_format"]["json_schema"]["name"] == "business_spec"
    assert first["response_format"]["json_schema"]["strict"] is False
    assert first["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert len(fake.calls[1]["messages"]) == 4


def test_structured_live_raises_after_two_invalid(live_mode, monkeypatch):
    _fake(monkeypatch, "nope")
    with pytest.raises(muse.MuseError):
        muse.structured("p", {"type": "object"}, "x")


def test_chat_live_parses_tool_calls(live_mode, monkeypatch):
    fake = _fake(monkeypatch, (None, [{"id": "call_1", "name": "crear_pedido", "arguments": '{"items": [{"product_id": "a", "qty": 2}]}'}]))
    r = muse.chat([{"role": "user", "content": "quiero 2"}], [{"name": "crear_pedido", "parameters": {"type": "object"}}])
    assert r == {"content": None, "tool_calls": [{"id": "call_1", "name": "crear_pedido", "arguments": {"items": [{"product_id": "a", "qty": 2}]}}]}
    body = fake.calls[0]
    assert body["extra_body"] == {"reasoning_effort": settings.model_reasoning}
    assert body["tools"][0]["type"] == "function" and body["tools"][0]["function"]["name"] == "crear_pedido"


@respx.mock
def test_image_generate_and_edit_live(live_mode):
    png = muse.mock_png(16, 16, (1, 2, 3), (4, 5, 6))
    gen = respx.post(f"{BASE}/images/generations").mock(return_value=httpx.Response(200, json={"created": 0, "data": [{"b64_json": base64.b64encode(png).decode()}]}))
    assert muse.image_generate("logo", "16:9") == png
    body = json.loads(gen.calls[0].request.content)
    assert body["model"] == settings.model_image and body["size"] == "1024x576" and body["output_format"] == "png"
    edit = respx.post(f"{BASE}/images/edits").mock(return_value=httpx.Response(200, json={"data": [{"url": "https://cdn.example/x.png"}]}))
    respx.get("https://cdn.example/x.png").mock(return_value=httpx.Response(200, content=png))
    assert muse.image_edit(png, "fondo blanco") == png
    ebody = json.loads(edit.calls[0].request.content)
    assert ebody["images"][0]["image_url"].startswith("data:image/png;base64,")

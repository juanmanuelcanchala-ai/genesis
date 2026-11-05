"""Tests for genesis.whatsapp: dry-run recorder, payload shapes, live calls via respx, webhooks."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import httpx
import pytest
import respx

from genesis import whatsapp as wa
from genesis.config import settings as real_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
GRAPH = f"https://graph.facebook.com/{real_settings.graph_version}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("WA_DRY_RUN", "1")
    monkeypatch.delenv("WA_FLOW_DATA_AS_STRING", raising=False)
    monkeypatch.delenv("WA_FLOW_MODE", raising=False)
    monkeypatch.setattr(wa, "settings", dataclasses.replace(real_settings, wa_token="", app_secret=""))
    wa.reset()
    yield
    wa.reset()


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("WA_DRY_RUN", "0")
    monkeypatch.setenv("WA_APP_ID", "app123")
    monkeypatch.setattr(wa, "settings", dataclasses.replace(real_settings, wa_token="TOKEN", waba_id="waba1"))
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    monkeypatch.setattr(wa, "_client", None)
    assert wa.settings.wa_live
    with respx.mock(assert_all_called=False) as router:
        yield router


# ------------------------------------------------------------------ dry-run payloads


def test_dry_run_records_and_returns_deterministic_ids():
    assert not wa.settings.wa_live
    a = wa.send_text("cradle-1", "573001112233", "Hola Rosa")
    b = wa.send_text("cradle-1", "573001112233", "Otra")
    assert a == "wamid.dry.text.1" and b == "wamid.dry.text.2"
    rec = wa.SENT[0]
    assert rec["op"] == "text" and rec["id"] == a
    p = rec["payload"]
    assert p["messaging_product"] == "whatsapp" and p["to"] == "573001112233"
    assert p["type"] == "text" and p["text"]["body"] == "Hola Rosa"
    assert p["phone_id"] == "cradle-1"


def test_buttons_payload_truncates_and_limits_to_three():
    wa.send_buttons("p", "57", "¿Confirmas?", [("yes", "Sí, dale"), ("no", "No"), ("x", "Un título larguísimo de más"), ("z", "Z")])
    inter = wa.sent("buttons")[0]["payload"]["interactive"]
    assert inter["type"] == "button"
    btns = inter["action"]["buttons"]
    assert len(btns) == 3
    assert btns[0] == {"type": "reply", "reply": {"id": "yes", "title": "Sí, dale"}}
    assert all(len(b["reply"]["title"]) <= 20 for b in btns)


def test_flow_payload_navigate_v3():
    wa.send_flow("p", "57", "Haz tu pedido", "flow-9", "Hacer pedido", "ORDER", {"name": "Rosa"}, "tok-1")
    inter = wa.sent("flow")[0]["payload"]["interactive"]
    assert inter["type"] == "flow"
    assert inter["action"]["name"] == "flow"
    params = inter["action"]["parameters"]
    assert params["flow_message_version"] == "3"
    assert params["flow_id"] == "flow-9"
    assert params["flow_cta"] == "Hacer pedido"
    assert params["flow_token"] == "tok-1"
    assert params["flow_action"] == "navigate"
    assert params["flow_action_payload"] == {"screen": "ORDER", "data": {"name": "Rosa"}}
    assert "mode" not in params


def test_flow_payload_without_data_and_string_mode(monkeypatch):
    wa.send_flow("p", "57", "b", "f", "Pedir", "ORDER")
    params = wa.sent("flow")[0]["payload"]["interactive"]["action"]["parameters"]
    assert params["flow_action_payload"] == {"screen": "ORDER"}
    assert params["flow_token"] == "unused"
    monkeypatch.setenv("WA_FLOW_DATA_AS_STRING", "1")
    monkeypatch.setenv("WA_FLOW_MODE", "draft")
    wa.send_flow("p", "57", "b", "f", "Pedir", "ORDER", {"a": 1})
    params = wa.sent("flow")[1]["payload"]["interactive"]["action"]["parameters"]
    assert params["flow_action_payload"]["data"] == '{"a": 1}'
    assert params["mode"] == "draft"


def test_cta_url_payload():
    wa.send_cta_url("p", "57", "Paga aquí", "Pagar", "https://mp.example/x")
    inter = wa.sent("cta_url")[0]["payload"]["interactive"]
    assert inter == {
        "type": "cta_url",
        "body": {"text": "Paga aquí"},
        "action": {"name": "cta_url", "parameters": {"display_text": "Pagar", "url": "https://mp.example/x"}},
    }


def test_image_uploads_then_sends_by_media_id():
    mid = wa.send_image("p", "57", PNG, "Tu logo")
    up = wa.sent("upload_media")[0]
    assert up["payload"]["mime"] == "image/png"
    img = wa.sent("image")[0]
    assert img["id"] == mid
    assert img["payload"]["image"] == {"id": up["id"], "caption": "Tu logo"}
    # dry-run media round trip
    assert wa.download_media(up["id"]) == (PNG, "image/png")


def test_template_payload():
    wa.send_template("p", "57", "negocio_vivo", "es", ["Rosa", "https://wa.me/57"])
    t = wa.sent("template")[0]["payload"]["template"]
    assert t["name"] == "negocio_vivo" and t["language"] == {"code": "es"}
    assert t["components"][0]["parameters"][1] == {"type": "text", "text": "https://wa.me/57"}


def test_mark_read_payload():
    wa.mark_read("p", "wamid.X")
    assert wa.sent("mark_read")[0]["payload"]["status"] == "read"


def test_dry_profile_roundtrip_and_limits():
    before = wa.get_business_profile("cradle-1")
    assert before["about"] == ""
    wa.update_business_profile("cradle-1", about="A" * 200, description="Arepas", address="San Fernando, Cali",
                               vertical="restaurant", websites=["https://x.co", "ftp://no", "http://a", "http://b"])
    rec = wa.sent("update_business_profile")[0]["payload"]
    assert len(rec["about"]) == 139 and rec["vertical"] == "RESTAURANT"
    assert rec["websites"] == ["https://x.co", "http://a"]
    after = wa.get_business_profile("cradle-1")
    assert after["description"] == "Arepas" and after["address"] == "San Fernando, Cali"
    wa.update_business_profile("cradle-1", vertical="FOOD")
    assert wa.get_business_profile("cradle-1")["vertical"] == "OTHER"


def test_dry_flow_lifecycle():
    fid = wa.create_flow("waba", "Pedidos Arepas Rosa", ["OTHER"])
    assert wa.update_flow_json(fid, {"version": "7.0", "screens": []}) == []
    wa.publish_flow(fid)
    wa.deprecate_or_delete_flow(fid)
    assert wa.sent("deprecate_flow") and not wa.sent("delete_flow")
    fid2 = wa.create_flow("waba", "Borrador", ["OTHER"])
    wa.deprecate_or_delete_flow(fid2)
    assert wa.sent("delete_flow")[0]["payload"]["flow_id"] == fid2


def test_dry_product_price_minor_units():
    pid = wa.create_product("cat", "arepa-choclo", "Arepa de choclo", "Con queso", 5000, "https://i/x.png", "https://u")
    p = wa.sent("create_product")[0]["payload"]
    assert pid.startswith("product.dry") and p["price"] == 500000 and p["currency"] == "COP"
    wa.delete_product("cat", "arepa-choclo")
    assert wa.sent("delete_product")[0]["payload"]["retailer_id"] == "arepa-choclo"


def test_dry_profile_picture_handle(monkeypatch):
    monkeypatch.setenv("WA_APP_ID", "app9")
    h = wa.upload_profile_picture("", PNG, "image/png")
    assert h.startswith("handle.dry")
    assert wa.sent("upload_profile_picture")[0]["payload"]["app_id"] == "app9"


def test_wa_me_link():
    assert wa.wa_me_link("+57 300 111 2233") == "https://wa.me/573001112233"
    assert wa.wa_me_link("573001112233", "Hola, quiero arepas") == "https://wa.me/573001112233?text=Hola%2C%20quiero%20arepas"


# ------------------------------------------------------------------ signature


def test_verify_signature(monkeypatch):
    body = b'{"object":"whatsapp_business_account"}'
    assert wa.verify_signature(body, "") is True  # no secret configured, dry-run
    monkeypatch.setenv("WA_DRY_RUN", "0")
    monkeypatch.setattr(wa, "settings", dataclasses.replace(real_settings, wa_token="TOKEN", app_secret=""))
    assert wa.verify_signature(body, "") is False  # live without a secret fails closed
    monkeypatch.setenv("WA_DRY_RUN", "1")
    monkeypatch.setattr(wa, "settings", dataclasses.replace(real_settings, app_secret="s3cret"))
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert wa.verify_signature(body, good)
    assert not wa.verify_signature(body + b" ", good)
    assert not wa.verify_signature(body, "sha256=deadbeef")
    assert not wa.verify_signature(body, "")


# ------------------------------------------------------------------ webhook parsing


def _wh(messages=None, statuses=None, phone_id="111"):
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000", "phone_number_id": phone_id}}
    if messages is not None:
        value["contacts"] = [{"profile": {"name": "Rosa"}, "wa_id": "573001112233"}]
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}]}


def _msg(mtype, body, mid="wamid.1"):
    return {"from": "573001112233", "id": mid, "timestamp": "1762250000", "type": mtype, mtype: body}


def test_parse_text_audio_image():
    out = wa.parse_webhook(_wh([
        _msg("text", {"body": "Hola"}, "m1"),
        _msg("audio", {"id": "aud1", "mime_type": "audio/ogg; codecs=opus", "voice": True}, "m2"),
        _msg("image", {"id": "img1", "mime_type": "image/jpeg", "caption": "mis arepas"}, "m3"),
    ]))
    assert [i.type for i in out] == ["text", "audio", "image"]
    t, a, im = out
    assert t.phone_id == "111" and t.wa_id == "573001112233" and t.name == "Rosa" and t.text == "Hola"
    assert t.timestamp == 1762250000 and t.message_id == "m1"
    assert a.media_id == "aud1" and a.mime == "audio/ogg"
    assert im.media_id == "img1" and im.mime == "image/jpeg" and im.text == "mis arepas"


def test_parse_interactive_replies_and_nfm():
    resp = {"flow_token": "tok", "qty_choclo": "2", "customer_name": "Ana"}
    out = wa.parse_webhook(_wh([
        _msg("interactive", {"type": "button_reply", "button_reply": {"id": "yes", "title": "Sí"}}, "b1"),
        _msg("interactive", {"type": "list_reply", "list_reply": {"id": "row2", "title": "Arepa", "description": "x"}}, "l1"),
        _msg("interactive", {"type": "nfm_reply", "nfm_reply": {"name": "flow", "body": "Sent", "response_json": json.dumps(resp)}}, "n1"),
        _msg("interactive", {"type": "nfm_reply", "nfm_reply": {"response_json": "{not json"}}, "n2"),
        _msg("button", {"payload": "PEDIR", "text": "Pedir"}, "t1"),
    ]))
    assert [i.type for i in out] == ["interactive"] * 4 + ["button"]
    assert out[0].button_id == "yes" and out[0].text == "Sí"
    assert out[1].button_id == "row2"
    assert out[2].flow_response == resp and out[2].button_id == ""
    assert out[3].flow_response == {}
    assert out[4].button_id == "PEDIR"


def test_parse_ignores_statuses_and_garbage():
    st = [{"id": "wamid.1", "status": "delivered", "timestamp": "1", "recipient_id": "57"}]
    assert wa.parse_webhook(_wh(statuses=st)) == []
    assert wa.parse_webhook({}) == []


def test_parse_logs_failed_statuses(caplog):
    st = [{"id": "wamid.2", "status": "failed", "recipient_id": "57",
           "errors": [{"code": 131047, "title": "Re-engagement message"}]}]
    with caplog.at_level("WARNING", logger="genesis.whatsapp"):
        assert wa.parse_webhook(_wh(statuses=st)) == []
    assert "131047" in caplog.text
    assert wa.parse_webhook({"entry": [{"changes": [{"value": {}}]}]}) == []


# ------------------------------------------------------------------ live mode (respx, no network)


def test_live_send_text_and_retry(live):
    route = live.post(f"{GRAPH}/PHONE/messages").mock(side_effect=[
        httpx.Response(429, json={"error": {"message": "slow down"}}),
        httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.REAL"}]}),
    ])
    assert wa.send_text("PHONE", "57", "Hola") == "wamid.REAL"
    assert route.call_count == 2
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer TOKEN"
    assert json.loads(req.content)["text"]["body"] == "Hola"
    assert wa.SENT == []


def test_live_post_not_retried_after_it_may_have_been_processed(live):
    """5xx or a read timeout on POST /messages may mean Graph already sent it: never send twice."""
    route = live.post(f"{GRAPH}/PHONE/messages").mock(side_effect=[
        httpx.Response(500, json={"error": {"message": "boom"}}),
        httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.TWICE"}]}),
    ])
    with pytest.raises(wa.WhatsAppError):
        wa.send_text("PHONE", "57", "Hola")
    assert route.call_count == 1
    flows = live.post(f"{GRAPH}/waba1/flows").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(wa.WhatsAppError):
        wa.create_flow("waba1", "Pedido", ["OTHER"])
    assert flows.call_count == 1
    conn = live.post(f"{GRAPH}/PHONE2/messages").mock(side_effect=[
        httpx.ConnectError("down"),
        httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]}),
    ])
    assert wa.send_text("PHONE2", "57", "x") == "wamid.OK"  # never reached Graph: safe to retry
    assert conn.call_count == 2
    prof = live.get(f"{GRAPH}/PHONE/whatsapp_business_profile").mock(side_effect=[
        httpx.Response(503, json={}),
        httpx.Response(200, json={"data": [{"about": "x"}]}),
    ])
    wa.get_business_profile("PHONE")
    assert prof.call_count == 2  # GET is idempotent


def test_live_error_raises(live):
    live.post(f"{GRAPH}/PHONE/messages").mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(wa.WhatsAppError) as e:
        wa.send_text("PHONE", "57", "x")
    assert e.value.status == 400 and "bad" in str(e.value)


def test_live_flows_api(live):
    live.post(f"{GRAPH}/waba1/flows").mock(return_value=httpx.Response(200, json={"id": "F1"}))
    assets = live.post(f"{GRAPH}/F1/assets").mock(return_value=httpx.Response(200, json={"success": True, "validation_errors": [{"error": "X"}]}))
    pub = live.post(f"{GRAPH}/F1/publish").mock(return_value=httpx.Response(200, json={"success": True}))
    assert wa.create_flow("", "Pedidos", ["OTHER"]) == "F1"
    assert wa.update_flow_json("F1", {"version": "7.0"}) == [{"error": "X"}]
    body = assets.calls.last.request.content
    assert b'name="asset_type"' in body and b"FLOW_JSON" in body and b'filename="flow.json"' in body
    wa.publish_flow("F1")
    assert pub.called
    live.get(f"{GRAPH}/F1").mock(return_value=httpx.Response(200, json={"id": "F1", "status": "PUBLISHED"}))
    dep = live.post(f"{GRAPH}/F1/deprecate").mock(return_value=httpx.Response(200, json={"success": True}))
    wa.deprecate_or_delete_flow("F1")
    assert dep.called
    live.get(f"{GRAPH}/F2").mock(return_value=httpx.Response(200, json={"id": "F2", "status": "DRAFT"}))
    dele = live.delete(f"{GRAPH}/F2").mock(return_value=httpx.Response(200, json={"success": True}))
    wa.deprecate_or_delete_flow("F2")
    assert dele.called


def test_live_resumable_upload(live):
    s1 = live.post(f"{GRAPH}/app123/uploads").mock(return_value=httpx.Response(200, json={"id": "upload:SESS"}))
    s2 = live.post(f"{GRAPH}/upload:SESS").mock(return_value=httpx.Response(200, json={"h": "HANDLE"}))
    assert wa.upload_profile_picture("", PNG, "image/png") == "HANDLE"
    q = s1.calls.last.request.url.params
    assert q["file_length"] == str(len(PNG)) and q["file_type"] == "image/png"
    r2 = s2.calls.last.request
    assert r2.headers["Authorization"] == "OAuth TOKEN" and r2.headers["file_offset"] == "0"
    assert r2.content == PNG


def test_live_profile_get_update(live):
    live.get(f"{GRAPH}/PHONE/whatsapp_business_profile").mock(return_value=httpx.Response(200, json={
        "data": [{"about": "Hola", "vertical": "OTHER", "messaging_product": "whatsapp"}]}))
    post = live.post(f"{GRAPH}/PHONE/whatsapp_business_profile").mock(return_value=httpx.Response(200, json={"success": True}))
    assert wa.get_business_profile("PHONE") == {"about": "Hola", "vertical": "OTHER"}
    wa.update_business_profile("PHONE", about="Arepas Rosa", profile_picture_handle="H")
    sent = json.loads(post.calls.last.request.content)
    assert sent == {"messaging_product": "whatsapp", "about": "Arepas Rosa", "profile_picture_handle": "H"}


def test_live_media_and_catalog(live):
    live.post(f"{GRAPH}/PHONE/media").mock(return_value=httpx.Response(200, json={"id": "MID"}))
    assert wa.upload_media("PHONE", PNG, "image/png") == "MID"
    live.get(f"{GRAPH}/MID").mock(return_value=httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/x", "mime_type": "audio/ogg; codecs=opus"}))
    live.get("https://lookaside.fbsbx.com/x").mock(return_value=httpx.Response(200, content=b"OGG"))
    assert wa.download_media("MID") == (b"OGG", "audio/ogg")
    prod = live.post(f"{GRAPH}/CAT/products").mock(return_value=httpx.Response(200, json={"id": "P1"}))
    assert wa.create_product("CAT", "r1", "Arepa", "Rica", 5000, "https://i", "https://u") == "P1"
    assert b"price=500000" in prod.calls.last.request.content
    batch = live.post(f"{GRAPH}/CAT/items_batch").mock(return_value=httpx.Response(200, json={"handles": ["h"]}))
    wa.delete_product("CAT", "r1")
    assert b"PRODUCT_ITEM" in batch.calls.last.request.content


def test_bsuid_sender_and_recipient():
    """Users with a WhatsApp username arrive with from_user_id only, and are answered in "recipient"."""
    from genesis import whatsapp as wa

    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "111"},
        "contacts": [{"profile": {"name": "Juan", "username": "JuanMa"}, "user_id": "CO.1780587866578965"}],
        "messages": [{"from_user_id": "CO.1780587866578965", "id": "wamid.x", "timestamp": "1", "type": "text",
                      "text": {"body": "hola"}}]}, "field": "messages"}]}]}
    inb = wa.parse_webhook(payload)[0]
    assert inb.wa_id == "CO.1780587866578965" and inb.name == "Juan"
    assert wa.is_bsuid(inb.wa_id) and not wa.is_bsuid("573001112233")
    wa.reset()
    wa.send_text("111", inb.wa_id, "hola")
    p = wa.SENT[-1]["payload"]
    assert p["recipient"] == "CO.1780587866578965" and "to" not in p

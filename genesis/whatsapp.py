"""WhatsApp Cloud API client (Graph API) with a dry-run recorder.

When ``not settings.wa_live`` (no WA_TOKEN or WA_DRY_RUN=1) no request leaves the process: every call
is appended to ``SENT`` as ``{"op", "payload", "id"}`` and a deterministic fake id is returned, so the
whole pipeline and its tests run offline.

Official references used for the payloads:
- Messages: https://developers.facebook.com/docs/whatsapp/cloud-api/messages
- Flows (send + Flows API): https://developers.facebook.com/docs/whatsapp/flows
- Business profile: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/business-profiles
- Resumable Upload: https://developers.facebook.com/docs/graph-api/guides/upload
- Catalog products / batch: https://developers.facebook.com/docs/marketing-api/reference/product-catalog
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import settings

log = logging.getLogger("genesis.whatsapp")

# ---------------------------------------------------------------------------------------------
# Dry-run recorder
# ---------------------------------------------------------------------------------------------

SENT: list[dict[str, Any]] = []
# media_id -> (bytes, mime). upload_media in dry-run stores here; tests/demo can pre-load inbound media.
DRY_MEDIA: dict[str, tuple[bytes, str]] = {}
# phone_id -> profile dict, so get/update/revert behave consistently in dry-run.
DRY_PROFILES: dict[str, dict[str, Any]] = {}
# flow_id -> status (DRAFT | PUBLISHED | DEPRECATED | DELETED)
DRY_FLOWS: dict[str, str] = {}

_lock = threading.Lock()

ABOUT_MAX = 139
ADDRESS_MAX = 256
DESCRIPTION_MAX = 512
BUTTON_TITLE_MAX = 20
FLOW_CTA_MAX = 30
VERTICALS = {
    "ALCOHOL", "APPAREL", "AUTO", "BEAUTY", "EDU", "ENTERTAIN", "EVENT_PLAN", "FINANCE", "GOVT",
    "GROCERY", "HEALTH", "HOTEL", "NONPROFIT", "ONLINE_GAMBLING", "OTC_DRUGS", "OTHER",
    "PHYSICAL_GAMBLING", "PROF_SERVICES", "RESTAURANT", "RETAIL", "TRAVEL",
}
PROFILE_FIELDS = "about,address,description,email,profile_picture_url,websites,vertical"


def reset() -> None:
    """Clear every dry-run store (tests)."""
    with _lock:
        SENT.clear()
        DRY_MEDIA.clear()
        DRY_PROFILES.clear()
        DRY_FLOWS.clear()


def _live() -> bool:
    return settings.wa_live


def _record(op: str, payload: dict[str, Any], prefix: str = "dry") -> str:
    """Record a dry-run call; the fake id is deterministic: prefix + op + position in SENT."""
    with _lock:
        fake = f"{prefix}.{op}.{len(SENT) + 1}"
        SENT.append({"op": op, "payload": payload, "id": fake})
    return fake


def sent(op: str | None = None) -> list[dict[str, Any]]:
    """Recorded calls, optionally filtered by op (test helper)."""
    with _lock:
        return [r for r in SENT if op is None or r["op"] == op]


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------


class WhatsAppError(RuntimeError):
    def __init__(self, status: int, body: Any, method: str = "", path: str = "") -> None:
        self.status = status
        self.body = body
        msg = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"Graph {method} {path} -> {status}: {msg}")


def _base() -> str:
    return f"https://graph.facebook.com/{settings.graph_version}"


_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
    return _client


def _request(method: str, path: str, *, retries: int = 2, auth: str | None = None,
             idempotent: bool | None = None, **kw: Any) -> Any:
    """Graph call with bearer auth. Returns parsed JSON.

    Always retried: 429 and connection failures (the request never reached Graph). Read timeouts and
    5xx are retried only for idempotent calls (GET/DELETE by default): a POST that Graph may already
    have processed (/messages, /{waba}/flows, /{catalog}/products) must not be sent twice.
    """
    if idempotent is None:
        idempotent = method.upper() in ("GET", "DELETE")
    url = path if path.startswith("http") else f"{_base()}/{path.lstrip('/')}"
    headers = dict(kw.pop("headers", {}) or {})
    headers.setdefault("Authorization", auth or f"Bearer {settings.wa_token}")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _http().request(method, url, headers=headers, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
            continue
        except httpx.TransportError as e:
            if not idempotent:
                raise WhatsAppError(0, f"{type(e).__name__}: {e}", method, path) from e
            last = e
            time.sleep(0.5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = WhatsAppError(r.status_code, _json(r), method, path)
            if attempt < retries and (r.status_code == 429 or idempotent):
                time.sleep(0.5 * (attempt + 1))
                continue
            raise last
        if r.status_code >= 400:
            raise WhatsAppError(r.status_code, _json(r), method, path)
        return _json(r)
    raise last or RuntimeError("unreachable")


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


# ---------------------------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------------------------


def _send(phone_id: str, to: str, msg_type: str, body: dict[str, Any], op: str) -> str:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": msg_type,
        msg_type: body,
    }
    if not _live():
        return _record(op, {"phone_id": phone_id, **payload}, prefix="wamid.dry")
    res = _request("POST", f"{phone_id}/messages", json=payload)
    return res["messages"][0]["id"]


def send_text(phone_id: str, to: str, body: str) -> str:
    return _send(phone_id, to, "text", {"preview_url": "http" in body, "body": body[:4096]}, "text")


def send_buttons(phone_id: str, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
    """Reply buttons: max 3, title <= 20 chars, unique ids."""
    if not buttons:
        raise ValueError("send_buttons needs at least one button")
    interactive = {
        "type": "button",
        "body": {"text": body[:1024]},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": bid[:256], "title": title[:BUTTON_TITLE_MAX]}}
                for bid, title in buttons[:3]
            ]
        },
    }
    return _send(phone_id, to, "interactive", interactive, "buttons")


def send_flow(
    phone_id: str,
    to: str,
    body: str,
    flow_id: str,
    cta: str,
    screen: str,
    data: dict | None = None,
    flow_token: str = "",
) -> str:
    """Interactive type=flow, flow_message_version "3", navigate action to `screen`.

    Unpublished flows are sent with mode=draft only when WA_FLOW_MODE=draft is set.
    `data` is sent as an object; WA_FLOW_DATA_AS_STRING=1 sends it serialized (the doc is ambiguous).
    """
    action_payload: dict[str, Any] = {"screen": screen}
    if data:  # must be non-empty if present
        as_string = os.getenv("WA_FLOW_DATA_AS_STRING", "0") not in ("0", "false", "no", "")
        action_payload["data"] = json.dumps(data, ensure_ascii=False) if as_string else data
    params: dict[str, Any] = {
        "flow_message_version": "3",
        "flow_token": flow_token or "unused",
        "flow_id": flow_id,
        "flow_cta": cta[:FLOW_CTA_MAX],
        "flow_action": "navigate",
        "flow_action_payload": action_payload,
    }
    mode = os.getenv("WA_FLOW_MODE", "")
    if mode in ("draft", "published"):
        params["mode"] = mode
    interactive = {
        "type": "flow",
        "body": {"text": body[:1024]},
        "action": {"name": "flow", "parameters": params},
    }
    return _send(phone_id, to, "interactive", interactive, "flow")


def send_cta_url(phone_id: str, to: str, body: str, button_text: str, url: str) -> str:
    interactive = {
        "type": "cta_url",
        "body": {"text": body[:1024]},
        "action": {"name": "cta_url", "parameters": {"display_text": button_text[:BUTTON_TITLE_MAX], "url": url}},
    }
    return _send(phone_id, to, "interactive", interactive, "cta_url")


def send_image(phone_id: str, to: str, image: bytes, caption: str = "") -> str:
    mime = _sniff_image(image)
    media_id = upload_media(phone_id, image, mime)
    body: dict[str, Any] = {"id": media_id}
    if caption:
        body["caption"] = caption[:1024]
    return _send(phone_id, to, "image", body, "image")


def send_template(phone_id: str, to: str, name: str, lang: str, params: list[str]) -> str:
    template: dict[str, Any] = {"name": name, "language": {"code": lang}}
    if params:
        template["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}
        ]
    return _send(phone_id, to, "template", template, "template")


def mark_read(phone_id: str, message_id: str) -> None:
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    if not _live():
        _record("mark_read", {"phone_id": phone_id, **payload})
        return
    try:
        _request("POST", f"{phone_id}/messages", json=payload, retries=0)
    except WhatsAppError:
        pass  # read receipts are best-effort


# ---------------------------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------------------------


def _sniff_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
    "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "video/mp4": "mp4",
    "application/pdf": "pdf",
}


def upload_media(phone_id: str, data: bytes, mime: str) -> str:
    if not _live():
        mid = _record("upload_media", {"phone_id": phone_id, "mime": mime, "size": len(data)}, prefix="media.dry")
        with _lock:
            DRY_MEDIA[mid] = (data, mime)
        return mid
    fname = f"file.{_EXT.get(mime, 'bin')}"
    res = _request(
        "POST",
        f"{phone_id}/media",
        data={"messaging_product": "whatsapp", "type": mime},
        files={"file": (fname, data, mime)},
    )
    return res["id"]


def download_media(media_id: str) -> tuple[bytes, str]:
    """(bytes, mime). Two steps: GET /{media_id} → url, then GET url with the same bearer."""
    if not _live():
        _record("download_media", {"media_id": media_id})
        with _lock:
            if media_id in DRY_MEDIA:
                return DRY_MEDIA[media_id]
        return (b"dry-run-media:" + media_id.encode(), "application/octet-stream")
    meta = _request("GET", media_id)
    url, mime = meta["url"], meta.get("mime_type", "application/octet-stream")
    r = _http().get(url, headers={"Authorization": f"Bearer {settings.wa_token}"}, follow_redirects=True)
    if r.status_code >= 400:
        raise WhatsAppError(r.status_code, r.text, "GET", "media-url")
    return r.content, mime.split(";")[0].strip()


# ---------------------------------------------------------------------------------------------
# Business profile
# ---------------------------------------------------------------------------------------------


def _default_profile() -> dict[str, Any]:
    return {"about": "", "address": "", "description": "", "email": "", "profile_picture_url": "",
            "websites": [], "vertical": "UNDEFINED"}


def get_business_profile(phone_id: str) -> dict:
    if not _live():
        _record("get_business_profile", {"phone_id": phone_id})
        with _lock:
            return dict(DRY_PROFILES.get(phone_id) or _default_profile())
    res = _request("GET", f"{phone_id}/whatsapp_business_profile", params={"fields": PROFILE_FIELDS})
    data = res.get("data") or [{}]
    prof = dict(data[0])
    prof.pop("messaging_product", None)
    return prof


def update_business_profile(
    phone_id: str,
    *,
    about: str | None = None,
    description: str | None = None,
    address: str | None = None,
    vertical: str | None = None,
    profile_picture_handle: str | None = None,
    websites: list[str] | None = None,
) -> None:
    body: dict[str, Any] = {"messaging_product": "whatsapp"}
    if about is not None:
        # The API rejects an empty `about` (1-139 chars), so reverting to "" falls back to a neutral text.
        body["about"] = (about.strip() or "Disponible")[:ABOUT_MAX]
    if description is not None:
        body["description"] = description[:DESCRIPTION_MAX]
    if address is not None:
        body["address"] = address[:ADDRESS_MAX]
    if vertical is not None:
        v = vertical.strip().upper()
        body["vertical"] = v if (v in VERTICALS or v == "") else "OTHER"
    if profile_picture_handle is not None:
        body["profile_picture_handle"] = profile_picture_handle
    if websites is not None:
        body["websites"] = [w for w in websites if w.startswith(("http://", "https://"))][:2]
    if not _live():
        _record("update_business_profile", {"phone_id": phone_id, **body})
        with _lock:
            prof = DRY_PROFILES.setdefault(phone_id, _default_profile())
            for k, v in body.items():
                if k == "messaging_product":
                    continue
                if k == "profile_picture_handle":
                    prof["profile_picture_url"] = f"dry://{v}"
                else:
                    prof[k] = v
        return
    _request("POST", f"{phone_id}/whatsapp_business_profile", json=body, idempotent=True)


def upload_profile_picture(app_id: str, data: bytes, mime: str) -> str:
    """Resumable Upload API → file handle `h` for profile_picture_handle.

    app_id falls back to WA_APP_ID. Step 1: POST /{app_id}/uploads?file_name&file_length&file_type.
    Step 2: POST /upload:<id> with `Authorization: OAuth <token>`, `file_offset: 0`, raw bytes.
    """
    app_id = app_id or os.getenv("WA_APP_ID", "")
    if mime == "image/jpg":
        mime = "image/jpeg"
    if not _live():
        return _record("upload_profile_picture", {"app_id": app_id, "mime": mime, "size": len(data)},
                       prefix="handle.dry")
    if not app_id:
        raise ValueError("upload_profile_picture needs an app id (WA_APP_ID)")
    fname = f"profile.{_EXT.get(mime, 'jpg')}"
    session = _request(
        "POST",
        f"{app_id}/uploads",
        params={"file_name": fname, "file_length": len(data), "file_type": mime},
    )
    upload_id = session["id"]  # "upload:<SESSION_ID>"
    res = _request(
        "POST",
        upload_id,
        auth=f"OAuth {settings.wa_token}",
        headers={"file_offset": "0", "Content-Type": mime},
        content=data,
    )
    return res["h"]


# ---------------------------------------------------------------------------------------------
# Flows API
# ---------------------------------------------------------------------------------------------


def create_flow(waba_id: str, name: str, categories: list[str]) -> str:
    waba_id = waba_id or settings.waba_id
    body = {"name": name, "categories": categories or ["OTHER"]}
    if not _live():
        fid = _record("create_flow", {"waba_id": waba_id, **body}, prefix="flow.dry")
        with _lock:
            DRY_FLOWS[fid] = "DRAFT"
        return fid
    res = _request("POST", f"{waba_id}/flows", json=body)
    return res["id"]


def update_flow_json(flow_id: str, flow_json: dict) -> list[dict]:
    """Upload flow.json as the FLOW_JSON asset; returns validation_errors ([] when valid)."""
    raw = json.dumps(flow_json, ensure_ascii=False).encode()
    if not _live():
        _record("update_flow_json", {"flow_id": flow_id, "flow_json": flow_json, "size": len(raw)})
        return []
    res = _request(
        "POST",
        f"{flow_id}/assets",
        data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        files={"file": ("flow.json", raw, "application/json")},
        idempotent=True,  # re-uploading the same FLOW_JSON asset replaces it
    )
    return list(res.get("validation_errors") or [])


def publish_flow(flow_id: str) -> None:
    if not _live():
        _record("publish_flow", {"flow_id": flow_id})
        with _lock:
            DRY_FLOWS[flow_id] = "PUBLISHED"
        return
    _request("POST", f"{flow_id}/publish", idempotent=True)


def flow_status(flow_id: str) -> str:
    if not _live():
        with _lock:
            return DRY_FLOWS.get(flow_id, "DRAFT")
    return _request("GET", flow_id, params={"fields": "status"}).get("status", "")


def deprecate_or_delete_flow(flow_id: str) -> None:
    """DRAFT → DELETE /{flow_id}; PUBLISHED/THROTTLED/BLOCKED → POST /{flow_id}/deprecate."""
    status = flow_status(flow_id)
    if status in ("DEPRECATED", "DELETED"):
        return
    if not _live():
        op = "delete_flow" if status == "DRAFT" else "deprecate_flow"
        _record(op, {"flow_id": flow_id, "status": status})
        with _lock:
            DRY_FLOWS[flow_id] = "DELETED" if status == "DRAFT" else "DEPRECATED"
        return
    if status == "DRAFT":
        _request("DELETE", flow_id)
    else:
        _request("POST", f"{flow_id}/deprecate", idempotent=True)


# ---------------------------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------------------------


def create_product(catalog_id: str, retailer_id: str, name: str, description: str, price_cop: int,
                   image_url: str, url: str) -> str:
    """POST /{catalog_id}/products. `price` is in minor units (2 digits for cents): 5000 COP → 500000."""
    body = {
        "retailer_id": retailer_id,
        "name": name[:150],
        "description": (description or name)[:5000],
        "price": int(price_cop) * 100,
        "currency": "COP",
        "availability": "in stock",
        "condition": "new",
        "image_url": image_url,
        "url": url,
    }
    if not _live():
        return _record("create_product", {"catalog_id": catalog_id, **body}, prefix="product.dry")
    res = _request("POST", f"{catalog_id}/products", data=body)
    return res["id"]


def delete_product(catalog_id: str, retailer_id: str) -> None:
    """Catalog Batch API: POST /{catalog_id}/items_batch with a DELETE request keyed by retailer_id."""
    requests = [{"method": "DELETE", "data": {"id": retailer_id}}]
    if not _live():
        _record("delete_product", {"catalog_id": catalog_id, "retailer_id": retailer_id})
        return
    _request(
        "POST",
        f"{catalog_id}/items_batch",
        data={"item_type": "PRODUCT_ITEM", "requests": json.dumps(requests)},
        idempotent=True,
    )


# ---------------------------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------------------------


def verify_signature(raw_body: bytes, header: str) -> bool:
    """X-Hub-Signature-256: "sha256=<hex HMAC-SHA256(app_secret, raw body)>".

    Without WA_APP_SECRET every webhook is accepted only in dry-run; live it fails closed.
    """
    secret = settings.app_secret
    if not secret:
        return not settings.wa_live
    if not header:
        return False
    sig = header.split("=", 1)[1] if header.startswith("sha256=") else header
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig.strip().lower())


@dataclass
class Inbound:
    phone_id: str  # metadata.phone_number_id (which of OUR numbers received it)
    wa_id: str  # sender
    name: str
    message_id: str
    type: str  # text|audio|image|interactive|button|...
    text: str = ""
    media_id: str = ""
    mime: str = ""
    button_id: str = ""
    flow_response: dict | None = None  # parsed nfm_reply.response_json
    timestamp: int = 0


def parse_webhook(payload: dict) -> list[Inbound]:
    """Flatten a messages webhook into Inbound items. Statuses and unknown shapes are ignored."""
    out: list[Inbound] = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_id = str((value.get("metadata") or {}).get("phone_number_id", ""))
            names = {
                str(c.get("wa_id", "")): (c.get("profile") or {}).get("name", "")
                for c in value.get("contacts") or []
            }
            for m in value.get("messages") or []:
                inb = _parse_message(phone_id, m, names)
                if inb is not None:
                    out.append(inb)
            for st in value.get("statuses") or []:
                if st.get("status") == "failed":  # e.g. 131047 (outside the 24 h window), 131030
                    err = (st.get("errors") or [{}])[0]
                    log.warning("message %s to %s failed on %s: %s %s", st.get("id"), st.get("recipient_id"),
                                phone_id, err.get("code"), err.get("title") or err.get("message"))
    return out


def _parse_message(phone_id: str, m: dict, names: dict[str, str]) -> Inbound | None:
    wa_id = str(m.get("from", ""))
    mtype = str(m.get("type", ""))
    try:
        ts = int(m.get("timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0
    inb = Inbound(phone_id=phone_id, wa_id=wa_id, name=names.get(wa_id, ""), message_id=str(m.get("id", "")),
                  type=mtype, timestamp=ts)
    if mtype == "text":
        inb.text = (m.get("text") or {}).get("body", "")
    elif mtype in ("audio", "image", "video", "document", "sticker", "voice"):
        media = m.get(mtype) or {}
        inb.media_id = str(media.get("id", ""))
        inb.mime = str(media.get("mime_type", "")).split(";")[0].strip()
        inb.text = media.get("caption", "") or ""
    elif mtype == "interactive":
        it = m.get("interactive") or {}
        kind = it.get("type", "")
        if kind in ("button_reply", "list_reply"):
            reply = it.get(kind) or {}
            inb.button_id = str(reply.get("id", ""))
            inb.text = reply.get("title", "")
        elif kind == "nfm_reply":
            nfm = it.get("nfm_reply") or {}
            raw = nfm.get("response_json")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except ValueError:
                parsed = {}
            inb.flow_response = parsed if isinstance(parsed, dict) else {}
            inb.text = nfm.get("body", "") or ""
    elif mtype == "button":  # quick-reply button from a template
        b = m.get("button") or {}
        inb.button_id = str(b.get("payload", ""))
        inb.text = b.get("text", "")
    elif mtype == "location":
        loc = m.get("location") or {}
        inb.text = loc.get("address") or loc.get("name") or f"{loc.get('latitude')},{loc.get('longitude')}"
    elif mtype == "reaction":
        inb.text = (m.get("reaction") or {}).get("emoji", "")
    return inb


def wa_me_link(display_number: str, text: str = "") -> str:
    digits = "".join(ch for ch in display_number if ch.isdigit())
    link = f"https://wa.me/{digits}"
    if text:
        link += "?text=" + quote(text, safe="")
    return link

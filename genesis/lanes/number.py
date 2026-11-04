"""Lane "number": the cradle number's WhatsApp business profile (text fields + profile picture)."""

from __future__ import annotations

import os
from typing import Any

from ..plan import Ctx, Node, PlanError
from ..spec import BusinessSpec, Fulfillment
from ._common import FOOD_WORDS, cfg, clip, cop, detail, hours_text, image_mime, mod, read_file

LANE = "number"
PROFILE_FIELDS = ("about", "description", "address", "vertical")

# WhatsApp business profile verticals (Graph API enum).
VERTICAL_WORDS = {
    "APPAREL": ("ropa", "zapato", "calzado", "moda", "bolso", "accesorio"),
    "BEAUTY": ("belleza", "uña", "una", "peluquer", "barber", "maquillaje", "spa", "cosmet"),
    "GROCERY": ("mercado", "fruta", "verdura", "tienda", "abarrote", "huevo"),
    "RETAIL": ("artesan", "juguete", "papeler", "regalo", "flor", "celular"),
    "PROF_SERVICES": ("servicio", "plomer", "electric", "costura", "arreglo", "clase"),
}


def vertical_for(spec: BusinessSpec) -> str:
    text = f"{spec.category} {' '.join(p.name for p in spec.products)}".lower()
    if any(w in text for w in FOOD_WORDS):
        return "RESTAURANT"
    for vertical, words in VERTICAL_WORDS.items():
        if any(w in text for w in words):
            return vertical
    return "OTHER"


def profile_fields(spec: BusinessSpec) -> dict[str, str]:
    place = spec.neighborhood or spec.city
    about = spec.about or f"{spec.category.capitalize()} en {place}. Pide por aquí y te lo tenemos listo."
    menu = ", ".join(f"{p.name} {cop(p.price_cop)}" for p in spec.products)
    how = {
        Fulfillment.pickup: "Recoges en el punto.",
        Fulfillment.delivery: f"Domicilios{' en ' + spec.delivery_zone if spec.delivery_zone else ''}.",
        Fulfillment.both: f"Recoges o te lo llevamos{' en ' + spec.delivery_zone if spec.delivery_zone else ''}.",
    }[spec.fulfillment]
    hours = hours_text(spec)
    description = spec.description or " ".join(
        x for x in [f"{spec.business_name}: {menu}.", f"Horario: {hours}." if hours else "", how,
                    "Pide por WhatsApp, te atiende el asistente de IA del negocio."] if x
    )
    address = spec.address_hint or ", ".join(x for x in [spec.neighborhood, spec.city] if x)
    return {
        "about": clip(about, 139),
        "description": clip(description, 512),
        "address": clip(address, 256),
        "vertical": vertical_for(spec),
    }


def _flat(profile: Any) -> dict[str, Any]:
    """get_business_profile may return the raw Graph envelope {"data": [{...}]} or the inner dict."""
    if isinstance(profile, dict) and isinstance(profile.get("data"), list):
        return profile["data"][0] if profile["data"] else {}
    return profile if isinstance(profile, dict) else {}


def apply_profile(ctx: Ctx) -> dict[str, Any]:
    wa = mod("whatsapp")
    try:
        previous = _flat(wa.get_business_profile(ctx.cradle_phone_id))
    except Exception as exc:  # noqa: BLE001 - a missing read only weakens revert, never blocks
        previous = {}
        detail(ctx, LANE, "number.profile", f"No pude leer el perfil anterior: {exc}")
    fields = profile_fields(ctx.spec)
    wa.update_business_profile(ctx.cradle_phone_id, **fields)
    detail(ctx, LANE, "number.profile", f"Perfil: {fields['about']}", profile=fields)
    return {**fields, "previous": {k: previous.get(k, "") for k in PROFILE_FIELDS}}


def verify_profile(ctx: Ctx, out: dict[str, Any]) -> bool:
    if not cfg().wa_live:
        return bool(out.get("about"))
    live = _flat(mod("whatsapp").get_business_profile(ctx.cradle_phone_id))
    return live.get("about") == out.get("about")


def revert_profile(ctx: Ctx, out: dict[str, Any]) -> None:
    prev = out.get("previous") or {}
    mod("whatsapp").update_business_profile(
        ctx.cradle_phone_id,
        about=prev.get("about") or None,  # about cannot be empty: leave ours if there was none
        description=prev.get("description", ""),
        address=prev.get("address", ""),
        vertical=prev.get("vertical") or None,
    )


def apply_photo(ctx: Ctx) -> dict[str, Any]:
    logo = ctx.out("brand.logo")
    data = read_file(logo.get("file"))
    if not data:
        return {"skipped": "sin logo"}
    wa = mod("whatsapp")
    app_id = getattr(cfg(), "app_id", "") or os.getenv("WA_APP_ID", "")
    if cfg().wa_live and not app_id:
        raise PlanError("falta WA_APP_ID para la Resumable Upload API")
    handle = wa.upload_profile_picture(app_id, data, image_mime(data))
    wa.update_business_profile(ctx.cradle_phone_id, profile_picture_handle=handle)
    detail(ctx, LANE, "number.photo", "Logo puesto como foto de perfil", image=logo.get("path"))
    return {"handle": handle, "image": logo.get("path")}


def nodes(ctx: Ctx) -> list[Node]:
    return [
        Node("number.profile", LANE, "Perfil del número", apply_profile, verify=verify_profile, revert=revert_profile),
        Node("number.photo", LANE, "Foto de perfil", apply_photo, deps=["brand.logo"], retries=1, critical=False),
    ]

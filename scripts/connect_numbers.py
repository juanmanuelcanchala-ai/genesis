"""Point two Cloud API numbers of a WABA at Génesis: one intake number and one or more cradles.

  uv run python scripts/connect_numbers.py                      # list the WABA's numbers
  uv run python scripts/connect_numbers.py --intake ID --cradle ID --url https://x.trycloudflare.com
  uv run python scripts/connect_numbers.py --restore ID [ID...]  # remove the per-number webhook override

Uses the per-number webhook override of the WhatsApp Cloud API, so only these numbers are routed to
Génesis and every other number of the WABA keeps its normal webhook. Writes the ids into .env.
Never prints the token.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
load_dotenv(ENV)

TOKEN = os.getenv("WA_TOKEN", "")
WABA = os.getenv("WABA_ID", "")
VER = os.getenv("GRAPH_VERSION", "v24.0")
VERIFY = os.getenv("WA_VERIFY_TOKEN", "genesis-verify")
G = f"https://graph.facebook.com/{VER}"


def call(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, f"{G}/{path}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30, **kw)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        err = data.get("error", {})
        sys.exit(f"Graph {method} /{path} -> {r.status_code}: {err.get('message', data)} (code {err.get('code')})")
    return data


def set_env(values: dict[str, str]) -> None:
    text = ENV.read_text() if ENV.exists() else ""
    for k, v in values.items():
        line = f"{k}={v}"
        text = re.sub(rf"^{k}=.*$", line, text, flags=re.M) if re.search(rf"^{k}=", text, flags=re.M) else text.rstrip("\n") + f"\n{line}\n"
    ENV.write_text(text)
    os.chmod(ENV, 0o600)


def numbers() -> list[dict]:
    fields = "id,display_phone_number,verified_name,status,quality_rating,code_verification_status,platform_type,webhook_configuration"
    return call("GET", f"{WABA}/phone_numbers", params={"fields": fields, "limit": 100}).get("data", [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intake")
    ap.add_argument("--cradle", action="append", default=[])
    ap.add_argument("--url", help="public base URL of the Génesis server (https)")
    ap.add_argument("--restore", nargs="*")
    a = ap.parse_args()
    if not TOKEN or not WABA:
        sys.exit("Falta WA_TOKEN o WABA_ID en .env")

    nums = {n["id"]: n for n in numbers()}
    if a.restore is not None:
        for pid in a.restore:
            call("POST", pid, json={"webhook_configuration": {"override_callback_uri": ""}})
            print(f"override removido de {pid}")
        return
    if not a.intake:
        for n in nums.values():
            hook = (n.get("webhook_configuration") or {}).get("phone_number") or "-"
            print(f"{n['id']:>18}  {n.get('display_phone_number',''):<18} {n.get('verified_name',''):<28} {n.get('status',''):<12} {n.get('platform_type',''):<10} webhook={hook}")
        return
    if not a.url or not a.url.startswith("https://"):
        sys.exit("--url debe ser https (por ejemplo el túnel de cloudflared)")
    ids = [a.intake, *a.cradle]
    for pid in ids:
        if pid not in nums:
            sys.exit(f"{pid} no está en la WABA {WABA}")
        if nums[pid].get("platform_type") != "CLOUD_API":
            print(f"aviso: {pid} tiene platform_type={nums[pid].get('platform_type')}; debe estar registrado en Cloud API")

    call("POST", f"{WABA}/subscribed_apps")  # the app must be subscribed to the WABA for overrides to work
    callback = a.url.rstrip("/") + "/webhook"
    for pid in ids:
        call("POST", pid, json={"webhook_configuration": {"override_callback_uri": callback, "verify_token": VERIFY}})
        got = call("GET", pid, params={"fields": "webhook_configuration"}).get("webhook_configuration", {})
        print(f"{pid} -> {got.get('phone_number', got)}")

    set_env({
        "WA_INTAKE_PHONE_ID": a.intake,
        "WA_CRADLE_PHONE_IDS": ",".join(a.cradle),
        "WA_CRADLE_DISPLAY_NUMBERS": ",".join(re.sub(r"\D", "", nums[c].get("display_phone_number", "")) for c in a.cradle),
        "PUBLIC_BASE_URL": a.url.rstrip("/"),
        "WA_DRY_RUN": "0",
    })
    print("listo: .env actualizado (WA_INTAKE_PHONE_ID, WA_CRADLE_*, PUBLIC_BASE_URL, WA_DRY_RUN=0)")


if __name__ == "__main__":
    main()

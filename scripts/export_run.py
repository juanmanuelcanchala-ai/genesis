"""Export a live run from the running server into the video's data and assets.

    uv run python scripts/export_run.py [run_id]      # default: the latest run

Writes media/rehearsal/run.json (same shape as scripts/rehearse_real.py) and copies the run's logo, hero
shot and SAM crops into video/assets/. Reads GENESIS_ADMIN_TOKEN and the port from .env / PORT.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
BASE = f"http://127.0.0.1:{os.getenv('PORT', '8000')}"
H = {"X-Genesis-Token": os.getenv("GENESIS_ADMIN_TOKEN", "")}


def main() -> None:
    rid = sys.argv[1] if len(sys.argv) > 1 else httpx.get(f"{BASE}/api/runs", headers=H, timeout=10).json()[0]["run_id"]
    data = httpx.get(f"{BASE}/api/runs/{rid}/export", headers=H, timeout=30).raise_for_status().json()
    out = ROOT / "media" / "rehearsal" / "run.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    media = Path(os.getenv("GENESIS_MEDIA_DIR", str(ROOT / "media"))) / rid
    assets = ROOT / "video" / "assets"
    copied = []
    for src, dst in (("logo.png", "logo.png"), ("hero.png", "hero.png"), ("crop_0.png", "crop_1_0.png"), ("crop_1.png", "crop_2_0.png")):
        for f in media.glob(Path(src).stem + ".*"):
            shutil.copy(f, assets / dst)
            copied.append(dst)
            break
    print(f"run {rid}: {len(data['lines'])} lines, {len(data['events'])} events -> {out}; assets: {copied}")


if __name__ == "__main__":
    main()

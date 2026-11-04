"""Narration per scene with ElevenLabs (character timestamps), from video/script.json.

    ELEVEN_KEY=... uv run python video/narrate.py [scene_id ...]

Writes video/build/audio/<id>.mp3 and <id>.json ({duration, words: [{w, start, end}]}). Skips scenes
already generated unless named on the command line.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "build" / "audio"


def words(alignment: dict) -> list[dict]:
    chars, st, en = alignment["characters"], alignment["character_start_times_seconds"], alignment["character_end_times_seconds"]
    out, cur, s0, e0 = [], "", None, None
    for c, s, e in zip(chars, st, en):
        if c.isspace():
            if cur:
                out.append({"w": cur, "start": round(s0, 3), "end": round(e0, 3)})
            cur, s0 = "", None
            continue
        if s0 is None:
            s0 = s
        cur, e0 = cur + c, e
    if cur:
        out.append({"w": cur, "start": round(s0, 3), "end": round(e0, 3)})
    return out


def main() -> None:
    key = os.environ["ELEVEN_KEY"]
    script = json.loads((HERE / "script.json").read_text())
    only = set(sys.argv[1:])
    OUT.mkdir(parents=True, exist_ok=True)
    for sc in script["scenes"]:
        mp3, meta = OUT / f"{sc['id']}.mp3", OUT / f"{sc['id']}.json"
        if meta.exists() and sc["id"] not in only:
            continue
        r = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{script['voice']}/with-timestamps?output_format=mp3_44100_128",
            headers={"xi-api-key": key},
            json={"text": sc["es"], "model_id": "eleven_multilingual_v2",
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.2, "use_speaker_boost": True}},
            timeout=120,
        )
        r.raise_for_status()
        d = r.json()
        mp3.write_bytes(base64.b64decode(d["audio_base64"]))
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)],
                                   capture_output=True, text=True).stdout)
        meta.write_text(json.dumps({"duration": round(dur, 3), "words": words(d["alignment"])}, ensure_ascii=False, indent=1))
        print(f"{sc['id']}: {dur:.1f} s", flush=True)


if __name__ == "__main__":
    main()

"""Timeline, audio, subtitles and the final cut of the Génesis presentation video.

    uv run python video/assemble.py timeline   # build/timeline.js + build/run.js (scenes read these)
    uv run python video/assemble.py final      # build/genesis.mp4 from build/scenes/*.mp4 + narration + subs

Scene durations come from the narration (build/audio/<id>.json, made by narrate.py): LEAD seconds of
silence, the narration, then TAIL. Scene s05 also plays Rosa's voice note at 2x after the narration.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
LEAD, TAIL = 0.4, 0.6
ROSA_SPEED = 2.0
EXTRA = {"s01": 0.6, "s15": 1.2}  # seconds added at the end of these scenes


def probe(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                                capture_output=True, text=True, check=True).stdout)


def build_timeline() -> dict:
    script = json.loads((HERE / "script.json").read_text())
    scenes, t = {}, 0.0
    rosa_dur = probe(HERE / "assets" / "rosa_voz.mp3") / ROSA_SPEED
    for sc in script["scenes"]:
        a = json.loads((BUILD / "audio" / f"{sc['id']}.json").read_text())
        words = [{"w": w["w"], "start": round(w["start"] + LEAD, 3), "end": round(w["end"] + LEAD, 3)} for w in a["words"]]
        dur = LEAD + a["duration"] + TAIL + EXTRA.get(sc["id"], 0.0)
        entry = {"start": round(t, 3), "narr_offset": LEAD, "narr_duration": a["duration"], "words": words,
                 "es": sc["es"], "en": sc["en"]}
        if sc.get("rosa_audio"):
            rs = round(LEAD + a["duration"] + 0.3, 3)
            entry["rosa"] = {"start": rs, "duration": round(rosa_dur, 3), "speed": ROSA_SPEED,
                             "source_duration": round(rosa_dur * ROSA_SPEED, 3)}
            dur = rs + rosa_dur + 0.5
        dur = max(dur, float(sc.get("min_render", 0)))
        entry["duration"] = round(dur, 3)
        scenes[sc["id"]] = entry
        t += entry["duration"]
    tl = {"fps": 30, "total": round(t, 3), "scenes": scenes}
    BUILD.mkdir(exist_ok=True)
    (BUILD / "timeline.js").write_text("window.TIMELINE = " + json.dumps(tl, ensure_ascii=False, indent=1) + ";\n")
    run = json.loads((ROOT / "media" / "rehearsal" / "run.json").read_text())
    (BUILD / "run.js").write_text("window.RUN = " + json.dumps(run, ensure_ascii=False) + ";\n")
    print(f"timeline: {len(scenes)} escenas, {t:.1f} s")
    for k, v in scenes.items():
        print(f"  {k} {v['start']:>6.1f} +{v['duration']:.1f}")
    return tl


def _ts(s: float) -> str:
    h, rem = divmod(max(0.0, s), 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{sec:05.2f}"


def _chunks(text: str, limit: int = 70) -> list[str]:
    """Split English subtitle text into readable chunks (sentences, then commas/colons)."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    for p in parts:
        while len(p) > limit:
            cut = max(p.rfind(", ", 0, limit), p.rfind(": ", 0, limit), p.rfind("; ", 0, limit))
            if cut < 25:
                cut = p.rfind(" ", 0, limit)
            out.append(p[: cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out


def subtitles(tl: dict) -> Path:
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Inter,40,&H00F4EFE6,&H00FFFFFF,&H00000000,&H9A0C110F,0,0,0,0,100,100,0,0,3,14,0,2,200,200,52,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for sc in tl["scenes"].values():
        chunks = _chunks(sc["en"])
        a = sc["start"] + sc["narr_offset"]
        span = sc["narr_duration"]
        total = sum(len(c) for c in chunks)
        t = a
        for i, c in enumerate(chunks):
            d = span * len(c) / total
            end = t + d + (0.25 if i == len(chunks) - 1 else -0.04)  # never two chunks on screen at once
            lines.append(f"Dialogue: 0,{_ts(t)},{_ts(end)},Sub,,0,0,0,,{c}")
            t += d
    p = BUILD / "subs.ass"
    p.write_text(head + "\n".join(lines) + "\n")
    return p


def audio(tl: dict) -> Path:
    inputs, filters, labels = [], [], []
    for i, (sid, sc) in enumerate(tl["scenes"].items()):
        inputs += ["-i", str(BUILD / "audio" / f"{sid}.mp3")]
        ms = int((sc["start"] + sc["narr_offset"]) * 1000)
        filters.append(f"[{len(inputs) // 2 - 1}:a]aresample=44100,adelay={ms}|{ms}[n{i}]")
        labels.append(f"[n{i}]")
        if "rosa" in sc:
            inputs += ["-i", str(HERE / "assets" / "rosa_voz.mp3")]
            ms = int((sc["start"] + sc["rosa"]["start"]) * 1000)
            filters.append(f"[{len(inputs) // 2 - 1}:a]aresample=44100,atempo={sc['rosa']['speed']},volume=0.95,adelay={ms}|{ms}[r{i}]")
            labels.append(f"[r{i}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,apad,atrim=0:{tl['total']:.3f},loudnorm=I=-16:TP=-1.5:LRA=11[out]")
    out = BUILD / "audio.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(filters),
                    "-map", "[out]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(out)], check=True)
    return out


def final() -> None:
    tl = build_timeline()
    missing = [s for s in tl["scenes"] if not (BUILD / "scenes" / f"{s}.mp4").exists()]
    if missing:
        sys.exit(f"faltan escenas renderizadas: {missing}")
    lst = BUILD / "concat.txt"
    lst.write_text("".join(f"file 'scenes/{s}.mp4'\n" for s in tl["scenes"]))
    silent = BUILD / "silent.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(silent)], check=True)
    a = audio(tl)
    subs = subtitles(tl)
    out = BUILD / "genesis.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(silent), "-i", str(a),
                    "-vf", f"subtitles={subs.name}", "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "slow",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)],
                   check=True, cwd=BUILD)
    print(f"{out}  {probe(out):.1f} s")


if __name__ == "__main__":
    {"timeline": build_timeline, "final": final}[sys.argv[1]]()

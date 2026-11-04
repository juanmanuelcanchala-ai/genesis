"""Smoke test of the real server process: start uvicorn, check the panel, start a demo run and
follow its SSE stream until run.live, then shut the server down.

    uv run python scripts/smoke_server.py [--port 8765]

Runs in mock + dry-run unless the environment says otherwise. Exit code 0 = OK.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    tmp = tempfile.mkdtemp(prefix="genesis-smoke-")
    env = {
        **os.environ,
        "GENESIS_MOCK": os.environ.get("GENESIS_MOCK", "1"),
        "WA_DRY_RUN": os.environ.get("WA_DRY_RUN", "1"),
        "GENESIS_DB": os.environ.get("GENESIS_DB", str(Path(tmp) / "genesis.db")),
        "GENESIS_MEDIA_DIR": os.environ.get("GENESIS_MEDIA_DIR", str(Path(tmp) / "media")),
    }
    cmd = [sys.executable, "-m", "uvicorn", "genesis.server:app", "--host", "127.0.0.1", "--port", str(args.port),
           "--log-level", "warning"]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env)
    t0 = time.time()

    def step(msg: str) -> None:
        print(f"+{int((time.time() - t0) * 1000):6d} ms  {msg}", flush=True)

    try:
        token = os.getenv("GENESIS_ADMIN_TOKEN", "")
        with httpx.Client(base_url=base, timeout=10, headers={"X-Genesis-Token": token} if token else None) as c:
            for _ in range(100):
                try:
                    h = c.get("/health")
                    if h.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if proc.poll() is not None:
                    print("server exited early", file=sys.stderr)
                    return 1
                time.sleep(0.1)
            else:
                print("server did not start", file=sys.stderr)
                return 1
            step(f"server up: /health {h.json()}")

            panel = c.get("/")
            assert panel.status_code == 200 and "<html" in panel.text.lower(), panel.status_code
            step(f"GET / -> panel ({len(panel.text)} bytes)")

            r = c.post("/api/runs?demo=1&auto_confirm=1")
            assert r.status_code == 201, r.text
            run_id = r.json()["run_id"]
            step(f"POST /api/runs?demo=1 -> run {run_id}")

            kinds: list[str] = []
            last: dict = {}
            with c.stream("GET", f"/api/runs/{run_id}/events", params={"close": 1, "plain": 1},
                          timeout=httpx.Timeout(args.timeout, read=args.timeout)) as s:
                for line in s.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    kinds.append(ev["kind"])
                    if ev["kind"] in ("run.started", "spec.ready", "plan.ready", "inspect.done", "run.live", "run.failed"):
                        step(f"SSE {ev['kind']}: {ev['message']}")
                    last = ev
                    if ev["kind"] in ("run.live", "run.failed"):
                        break
            if last.get("kind") != "run.live":
                print(f"run did not go live: {last}", file=sys.stderr)
                return 1
            done = kinds.count("node.done")
            step(f"SSE {len(kinds)} events, {done} node.done, wa.me {last['data'].get('wa_me', '')[:40]}...")
            info = c.get(f"/api/runs/{run_id}").json()
            assert info["state"] == "live", info
            biz = c.get("/api/businesses").json()
            assert any(b["live"] for b in biz), biz
            step(f"GET /api/runs/{run_id} -> {info['state']}; /api/businesses -> {len(biz)} live")
        print("SMOKE OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())

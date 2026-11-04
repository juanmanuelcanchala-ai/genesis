"""Plan and apply: a deterministic graph of Meta resources compiled from a BusinessSpec.

Terraform-style: each node has an idempotency key, explicit dependencies, an `apply` that returns
outputs, a `verify` that checks the real resource, and a `revert`. Independent nodes run in
parallel lanes. If a node fails after retries, everything already applied is reverted in reverse
topological order, so a half-built business never goes live.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import settings
from .events import Event, bus
from .spec import BusinessSpec


@dataclass
class Ctx:
    """Shared, thread-safe context for one run."""

    run_id: str
    spec: BusinessSpec
    photos: list[bytes] = field(default_factory=list)
    cradle_phone_id: str = ""
    cradle_display_number: str = ""
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def out(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.outputs.get(node_id, {}))

    def put(self, node_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.outputs[node_id] = data

    def emit(self, kind: str, lane: str = "", node: str = "", message: str = "", **data: Any) -> None:
        bus.emit(Event(self.run_id, kind, lane, node, message, data))


Apply = Callable[[Ctx], dict[str, Any]]
Verify = Callable[[Ctx, dict[str, Any]], bool]
Revert = Callable[[Ctx, dict[str, Any]], None]


@dataclass
class Node:
    id: str
    lane: str  # number | brand | menu | agent | payments | inspector | launch
    title: str  # human label shown on the panel
    apply: Apply
    deps: list[str] = field(default_factory=list)
    verify: Verify | None = None
    revert: Revert | None = None
    retries: int = 2
    critical: bool = True  # non-critical nodes may fail without reverting the run

    def key(self, ctx: Ctx) -> str:
        raw = json.dumps({"node": self.id, "cradle": ctx.cradle_phone_id, "spec": ctx.spec.model_dump(mode="json")}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class State:
    """SQLite store of applied nodes, keyed by idempotency key."""

    def __init__(self, path: str | None = None) -> None:
        self._db = sqlite3.connect(path or settings.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "create table if not exists applied(key text primary key, run_id text, node text, outputs text, ts real)"
            )
            self._db.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("select outputs from applied where key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, run_id: str, node: str, outputs: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "insert or replace into applied values(?,?,?,?,?)", (key, run_id, node, json.dumps(outputs, default=str), time.time())
            )
            self._db.commit()

    def drop(self, key: str) -> None:
        with self._lock:
            self._db.execute("delete from applied where key=?", (key,))
            self._db.commit()


class PlanError(RuntimeError):
    pass


def toposort(nodes: list[Node]) -> list[Node]:
    by_id = {n.id: n for n in nodes}
    for n in nodes:
        for d in n.deps:
            if d not in by_id:
                raise PlanError(f"{n.id} depends on unknown node {d}")
    order: list[Node] = []
    seen: set[str] = set()
    temp: set[str] = set()

    def visit(n: Node) -> None:
        if n.id in seen:
            return
        if n.id in temp:
            raise PlanError(f"cycle at {n.id}")
        temp.add(n.id)
        for d in n.deps:
            visit(by_id[d])
        temp.discard(n.id)
        seen.add(n.id)
        order.append(n)

    for n in nodes:
        visit(n)
    return order


def apply_plan(ctx: Ctx, nodes: list[Node], state: State | None = None, workers: int = 6) -> dict[str, dict[str, Any]]:
    """Run the graph with maximum parallelism. Returns outputs per node. Raises PlanError after reverting."""
    state = state or State()
    order = toposort(nodes)
    by_id = {n.id: n for n in order}
    done: set[str] = set()
    failed: set[str] = set()
    applied: list[Node] = []
    ctx.emit("plan.ready", message=f"{len(order)} nodes", nodes=[{"id": n.id, "lane": n.lane, "title": n.title, "deps": n.deps} for n in order])

    def run_node(n: Node) -> dict[str, Any]:
        key = n.key(ctx)
        cached = state.get(key)
        if cached is not None and (n.verify is None or _safe_verify(n, ctx, cached)):
            ctx.emit("node.done", n.lane, n.id, f"{n.title} (ya existía)", outputs=cached, cached=True)
            return cached
        last: Exception | None = None
        for attempt in range(n.retries + 1):
            ctx.emit("node.started", n.lane, n.id, n.title, attempt=attempt)
            t0 = time.time()
            try:
                out = n.apply(ctx) or {}
                if n.verify is not None and not n.verify(ctx, out):
                    raise PlanError(f"verify failed for {n.id}")
                state.put(key, ctx.run_id, n.id, out)
                ctx.emit("node.done", n.lane, n.id, n.title, outputs=_public(out), ms=int((time.time() - t0) * 1000))
                return out
            except Exception as exc:  # noqa: BLE001 - every failure is reported on the panel
                last = exc
                ctx.emit("node.retry" if attempt < n.retries else "node.failed", n.lane, n.id, f"{n.title}: {exc}", attempt=attempt)
                time.sleep(min(0.5 * 2**attempt, 4))
        raise PlanError(f"{n.id}: {last}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        running: dict[Any, Node] = {}
        while len(done) + len(failed) < len(order):
            ready = [
                n for n in order
                if n.id not in done and n.id not in failed and n not in running.values()
                and all(d in done for d in n.deps)
            ]
            blocked = [n for n in order if n.id not in done and n.id not in failed and any(d in failed for d in n.deps)]
            for n in blocked:
                failed.add(n.id)
                ctx.emit("node.skipped", n.lane, n.id, f"{n.title}: dependencia falló")
            for n in ready:
                running[pool.submit(run_node, n)] = n
            if not running:
                break
            finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for fut in finished:
                n = running.pop(fut)
                try:
                    ctx.put(n.id, fut.result())
                    done.add(n.id)
                    applied.append(n)
                except Exception as exc:  # noqa: BLE001
                    failed.add(n.id)
                    if n.critical:
                        for f in running:
                            f.cancel()
                        wait(list(running))
                        for f, rn in running.items():
                            if not f.cancelled() and f.exception() is None:
                                ctx.put(rn.id, f.result())
                                applied.append(rn)
                        _revert_all(ctx, applied, state)
                        ctx.emit("run.failed", message=str(exc))
                        raise PlanError(str(exc)) from exc
    return {nid: ctx.out(nid) for nid in done if nid in by_id}


def _revert_all(ctx: Ctx, applied: list[Node], state: State) -> None:
    for n in reversed(applied):
        if n.revert is None:
            continue
        try:
            n.revert(ctx, ctx.out(n.id))
            state.drop(n.key(ctx))
            ctx.emit("node.reverted", n.lane, n.id, f"revertido: {n.title}")
        except Exception as exc:  # noqa: BLE001
            ctx.emit("node.revert_failed", n.lane, n.id, f"{n.title}: {exc}")


def _safe_verify(n: Node, ctx: Ctx, out: dict[str, Any]) -> bool:
    try:
        return bool(n.verify and n.verify(ctx, out))
    except Exception:  # noqa: BLE001
        return False


def _public(out: dict[str, Any]) -> dict[str, Any]:
    """Outputs shown on the panel: drop bulky/binary values."""
    return {k: v for k, v in out.items() if not isinstance(v, (bytes, bytearray)) and not (isinstance(v, str) and len(v) > 2000)}

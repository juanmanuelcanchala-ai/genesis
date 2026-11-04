"""In-process event bus. The stage panel subscribes over SSE; tests read the log."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    run_id: str
    kind: str  # run.started | spec.ready | node.started | node.done | node.failed | node.reverted | inspect.* | run.live | run.failed | msg
    lane: str = ""
    node: str = ""
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def sse(self) -> str:
        return f"event: {self.kind}\ndata: {json.dumps(asdict(self), ensure_ascii=False, default=str)}\n\n"


class Bus:
    def __init__(self) -> None:
        self._subs: dict[str, list[queue.Queue[Event]]] = {}
        self._log: dict[str, list[Event]] = {}
        self._lock = threading.Lock()

    def emit(self, ev: Event) -> None:
        with self._lock:
            self._log.setdefault(ev.run_id, []).append(ev)
            subs = list(self._subs.get(ev.run_id, [])) + list(self._subs.get("*", []))
        for q in subs:
            q.put(ev)

    def subscribe(self, run_id: str = "*") -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue()
        with self._lock:
            self._subs.setdefault(run_id, []).append(q)
            backlog = list(self._log.get(run_id, [])) if run_id != "*" else []
        for ev in backlog:
            q.put(ev)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subs.get(run_id, []):
                self._subs[run_id].remove(q)

    def log(self, run_id: str) -> list[Event]:
        with self._lock:
            return list(self._log.get(run_id, []))


bus = Bus()

"""SQLite store for live businesses: cradle number -> spec + agent config, orders, conversations.

One shared connection guarded by a lock (FastAPI runs sync handlers in a threadpool). The DB file
is `settings.db_path` (overridable at runtime with GENESIS_DB or `reset(path)` in tests); plan.State
uses the same file with its own table, so we use WAL mode and a busy timeout.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from .config import settings
from .spec import BusinessSpec

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_path: str | None = None

_SCHEMA = """
create table if not exists businesses(
    cradle_phone_id text primary key,
    owner_wa_id text not null default '',
    spec text not null,
    flow_id text not null default '',
    system_prompt text not null default '',
    run_id text not null default '',
    live integer not null default 0,
    created_at real, updated_at real
);
create table if not exists orders(
    n integer primary key autoincrement,
    cradle_phone_id text not null,
    customer_wa_id text not null,
    status text not null default 'pending',
    payment_id text not null default '',
    test integer not null default 0,
    total_cop integer not null default 0,
    data text not null,
    created_at real, updated_at real
);
create index if not exists orders_cradle on orders(cradle_phone_id);
create table if not exists messages(
    id integer primary key autoincrement,
    cradle_phone_id text not null,
    wa_id text not null,
    role text not null,
    content text not null,
    ts real
);
create index if not exists messages_conv on messages(cradle_phone_id, wa_id, id);
create table if not exists seen(message_id text primary key, ts real);
"""


def _db() -> sqlite3.Connection:
    global _conn, _path
    with _lock:
        if _conn is None:
            _path = _path or os.getenv("GENESIS_DB") or settings.db_path
            _conn = sqlite3.connect(_path, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            if _path != ":memory:":
                _conn.execute("pragma journal_mode=wal")
            _conn.executescript(_SCHEMA)
            _conn.commit()
        return _conn


def reset(path: str | None = None) -> None:
    """Close the connection and point the store at `path` (tests, demo resets)."""
    global _conn, _path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _path = path


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        db = _db()
        cur = db.execute(sql, args)
        db.commit()
        return cur


def _all(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _db().execute(sql, args).fetchall()


# ---------------------------------------------------------------- businesses


def save_business(
    cradle_phone_id: str,
    owner_wa_id: str,
    spec: BusinessSpec,
    flow_id: str = "",
    system_prompt: str = "",
    run_id: str = "",
) -> None:
    """Insert or update a business. Keeps the current `live` flag (new businesses start not live)."""
    now = time.time()
    _exec(
        """insert into businesses(cradle_phone_id, owner_wa_id, spec, flow_id, system_prompt, run_id, live, created_at, updated_at)
           values(?,?,?,?,?,?,0,?,?)
           on conflict(cradle_phone_id) do update set owner_wa_id=excluded.owner_wa_id, spec=excluded.spec,
             flow_id=excluded.flow_id, system_prompt=excluded.system_prompt, run_id=excluded.run_id,
             updated_at=excluded.updated_at""",
        (cradle_phone_id, owner_wa_id, spec.model_dump_json(), flow_id, system_prompt, run_id, now, now),
    )


def _business(row: sqlite3.Row) -> dict:
    return {
        "cradle_phone_id": row["cradle_phone_id"],
        "spec": BusinessSpec.model_validate_json(row["spec"]),
        "flow_id": row["flow_id"],
        "system_prompt": row["system_prompt"],
        "owner_wa_id": row["owner_wa_id"],
        "live": bool(row["live"]),
        "run_id": row["run_id"],
    }


def get_business(cradle_phone_id: str) -> dict | None:
    rows = _all("select * from businesses where cradle_phone_id=?", (cradle_phone_id,))
    return _business(rows[0]) if rows else None


def list_businesses() -> list[dict]:
    """All businesses, newest first (panel / GET /api/businesses), same shape as get_business."""
    return [_business(row) for row in _all("select * from businesses order by updated_at desc")]


def set_live(cradle_phone_id: str, live: bool) -> None:
    _exec("update businesses set live=?, updated_at=? where cradle_phone_id=?", (int(live), time.time(), cradle_phone_id))


def delete_business(cradle_phone_id: str) -> None:
    """Free a cradle number (demo reset)."""
    _exec("delete from businesses where cradle_phone_id=?", (cradle_phone_id,))


# -------------------------------------------------------------------- orders


def _norm_id(order_id: str | int) -> int:
    s = str(order_id).strip().lstrip("#")
    if not s.isdigit():
        raise KeyError(f"order id inválido: {order_id!r}")
    return int(s)


def _order(row: sqlite3.Row) -> dict:
    data = json.loads(row["data"])
    data.update(
        {
            "id": f"#{row['n']}",
            "cradle_phone_id": row["cradle_phone_id"],
            "customer_wa_id": row["customer_wa_id"],
            "status": row["status"],
            "payment_id": row["payment_id"],
            "test": bool(row["test"]),
            "total_cop": row["total_cop"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )
    return data


_RESERVED = {"id", "cradle_phone_id", "customer_wa_id", "status", "payment_id", "test", "total_cop", "created_at", "updated_at"}


def create_order(cradle_phone_id: str, customer_wa_id: str, order: dict, test: bool = False) -> dict:
    """Persist a parsed order ({"items", "total_cop", ...}). Adds "id" ("#1", "#2"...) and status "pending".

    The total is recomputed from the items when they carry qty and unit_cop, so a tampered total never wins.
    """
    items = order.get("items") or []
    total = sum(int(i.get("qty", 0)) * int(i.get("unit_cop", 0)) for i in items) if items else int(order.get("total_cop") or 0)
    data = {k: v for k, v in order.items() if k not in _RESERVED}
    data["items"] = items
    now = time.time()
    with _lock:
        cur = _exec(
            "insert into orders(cradle_phone_id, customer_wa_id, status, test, total_cop, data, created_at, updated_at) values(?,?,?,?,?,?,?,?)",
            (cradle_phone_id, customer_wa_id, "pending", int(test), total, json.dumps(data, ensure_ascii=False, default=str), now, now),
        )
        return get_order(f"#{cur.lastrowid}")  # type: ignore[return-value]


def get_order(order_id: str | int) -> dict | None:
    try:
        n = _norm_id(order_id)
    except KeyError:
        return None
    rows = _all("select * from orders where n=?", (n,))
    return _order(rows[0]) if rows else None


def set_order_status(order_id: str, status: str, payment_id: str = "") -> dict:
    n = _norm_id(order_id)
    with _lock:
        if payment_id:
            _exec("update orders set status=?, payment_id=?, updated_at=? where n=?", (status, payment_id, time.time(), n))
        else:
            _exec("update orders set status=?, updated_at=? where n=?", (status, time.time(), n))
        order = get_order(n)
    if order is None:
        raise KeyError(f"pedido {order_id} no existe")
    return order


def mark_paid_once(order_id: str, payment_id: str = "") -> bool:
    """Atomically move an order to 'paid'. True only for the call that did it (MP sends several
    notifications per payment, each handled in its own thread)."""
    n = _norm_id(order_id)
    cur = _exec("update orders set status='paid', payment_id=?, updated_at=? where n=? and status!='paid'",
                (payment_id, time.time(), n))
    return cur.rowcount == 1


def update_order(order_id: str, **fields: Any) -> dict:
    """Merge extra fields into the order's data (e.g. pay_url, preference_id)."""
    n = _norm_id(order_id)
    with _lock:
        rows = _all("select data from orders where n=?", (n,))
        if not rows:
            raise KeyError(f"pedido {order_id} no existe")
        data = json.loads(rows[0]["data"])
        data.update({k: v for k, v in fields.items() if k not in _RESERVED})
        _exec("update orders set data=?, updated_at=? where n=?", (json.dumps(data, ensure_ascii=False, default=str), time.time(), n))
        return get_order(n)  # type: ignore[return-value]


def list_orders(cradle_phone_id: str, customer_wa_id: str | None = None, include_test: bool = True) -> list[dict]:
    """Orders of a business, newest first."""
    sql = "select * from orders where cradle_phone_id=?"
    args: list[Any] = [cradle_phone_id]
    if customer_wa_id is not None:
        sql += " and customer_wa_id=?"
        args.append(customer_wa_id)
    if not include_test:
        sql += " and test=0"
    return [_order(r) for r in _all(sql + " order by n desc", tuple(args))]


# ------------------------------------------------------------- conversations


def append(cradle_phone_id: str, wa_id: str, role: str, content: str) -> None:
    _exec(
        "insert into messages(cradle_phone_id, wa_id, role, content, ts) values(?,?,?,?,?)",
        (cradle_phone_id, wa_id, role, content, time.time()),
    )


def history(cradle_phone_id: str, wa_id: str, limit: int = 20) -> list[dict]:
    """Last `limit` messages of a conversation, oldest first: [{"role", "content", "ts"}]."""
    rows = _all(
        "select role, content, ts from messages where cradle_phone_id=? and wa_id=? order by id desc limit ?",
        (cradle_phone_id, wa_id, limit),
    )
    return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in reversed(rows)]


def mark_seen(message_id: str) -> bool:
    """True the first time a WhatsApp message id is seen (webhooks are retried by Meta)."""
    if not message_id:
        return True
    with _lock:
        cur = _exec("insert or ignore into seen(message_id, ts) values(?,?)", (message_id, time.time()))
        return cur.rowcount == 1

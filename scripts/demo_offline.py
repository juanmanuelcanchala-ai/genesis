"""Offline rehearsal of the whole stage demo, no network, no keys.

    GENESIS_MOCK=1 WA_DRY_RUN=1 uv run python scripts/demo_offline.py

Rosa's voice note + photos arrive by webhook at the intake number, Génesis asks one question,
Rosa answers and says "sí", every lane runs, the inspector approves, the business goes live, a
customer asks a price, orders through the Flow, pays the (mock) link and Rosa is notified.
Prints a timeline with milliseconds.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("GENESIS_MOCK", "1")
os.environ.setdefault("WA_DRY_RUN", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genesis import demo  # noqa: E402


def main() -> int:
    try:
        res = demo.run()
    except Exception as exc:  # noqa: BLE001
        print(f"DEMO FALLÓ: {exc}", file=sys.stderr)
        return 1
    print("Génesis · demo offline (mock + dry-run)\n")
    print(res.timeline())
    order = res.paid or res.order
    print()
    print(f"run_id        {res.run_id}")
    print(f"pregunta      {res.question}")
    print(f"número cuna   {res.cradle}  {res.wa_me}")
    print(f"pedido        {order.get('id')} · ${order.get('total_cop', 0):,} · {order.get('status')}".replace(",", "."))
    print(f"mensajes      {sum(1 for s in res.sent if s['op'] in ('text', 'buttons', 'flow', 'cta_url'))} enviados por WhatsApp (dry-run)")
    print(f"total         {res.elapsed_ms} ms")
    return 0 if order.get("status") == "paid" else 1


if __name__ == "__main__":
    raise SystemExit(main())

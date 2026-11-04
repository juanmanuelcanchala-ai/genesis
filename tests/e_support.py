"""Shared helpers for module E tests (store, agent, payments, inspector)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from genesis import store, whatsapp
from genesis.spec import BusinessSpec

ROOT = Path(__file__).resolve().parent.parent
CRADLE = "cradle-1"
OWNER = "573001112233"
CUSTOMER = "573104445566"
_ids = itertools.count(1)


def rosa() -> BusinessSpec:
    return BusinessSpec.model_validate(json.loads((ROOT / "fixtures" / "rosa_spec.json").read_text()))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_MOCK", "1")
    monkeypatch.setenv("WA_DRY_RUN", "1")
    store.reset(str(tmp_path / "e.db"))
    whatsapp.reset()
    try:
        from genesis import muse

        muse.set_mock_spec(None)
    except Exception:  # noqa: BLE001
        pass
    yield tmp_path
    store.reset(None)
    whatsapp.reset()


def save_rosa(live: bool = True, flow_id: str = "flow-1", spec: BusinessSpec | None = None) -> BusinessSpec:
    from genesis import agent

    spec = spec or rosa()
    store.save_business(CRADLE, OWNER, spec, flow_id, agent.system_prompt(spec), "run-1")
    store.set_live(CRADLE, live)
    return spec


def inbound(**kw) -> whatsapp.Inbound:
    base = {"phone_id": CRADLE, "wa_id": CUSTOMER, "name": "Carlos", "message_id": f"wamid.test.{next(_ids)}", "type": "text"}
    base.update(kw)
    return whatsapp.Inbound(**base)

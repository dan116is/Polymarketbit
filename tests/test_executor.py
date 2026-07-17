"""M6 boundary tests: the live bot refuses without every lock open; the
shadow executor records real build+sign latency without sending anything."""
import asyncio
import json

import pytest

from polysignal.executor import LiveExecutor, ShadowExecutor
from polysignal.risk import RiskManager
from polysignal.store import Store
from tests.test_engine_guards import make_market

CFG = json.load(open("config.json"))


@pytest.fixture
def stack(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    risk = RiskManager(store, CFG["risk"])
    yield store, risk
    store.close()


def test_live_refusal_matrix(stack, monkeypatch):
    """Every lock must open, in order, before the bot may exist."""
    store, risk = stack
    cfg = json.loads(json.dumps(CFG))
    ex = LiveExecutor(store, risk, cfg)
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)

    assert ex.refusal("PAPER") == "not_armed"
    cfg["m6"]["armed"] = True
    assert ex.refusal("PAPER") == "gate1b_red"
    store.set_gate("GATE1B", True)
    assert ex.refusal("PAPER") == "gate2b_red"
    store.set_gate("GATE2B", True)
    assert ex.refusal("PAPER") == "no_credentials"
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "11" * 32)
    # risk gates (GATE1/GATE2 for LIVE mode) still red -> blocked
    assert ex.refusal("PAPER") == "risk_blocked"
    store.set_gate("GATE1", True)
    store.set_gate("GATE2", True)
    assert ex.refusal("PAPER") is None
    # kill switch closes everything again
    risk.kill(True)
    assert ex.refusal("PAPER") == "risk_blocked"


def test_live_fire_refuses_and_logs(stack):
    store, risk = stack
    ex = LiveExecutor(store, risk, json.loads(json.dumps(CFG)))

    class St:
        window_ts = 1784226900
        market = make_market(1784226900)
        book = {}
    from polysignal.quant import Signal
    out = asyncio.run(ex.fire(St(), Signal("UP", 1.0, 0.08, 0.6, "t"), "PAPER"))
    assert out == {"sent": False, "reason": "not_armed"}
    n = store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='LIVE_BOT_REFUSED'").fetchone()[0]
    assert n == 1


def test_shadow_records_real_build_latency(stack):
    """The shadow pipeline signs a REAL order with a throwaway key and logs
    the measured latency — nothing is ever sent."""
    store, risk = stack
    ex = ShadowExecutor(store, CFG)

    class St:
        window_ts = 1784226900
        market = make_market(1784226900)
        book = {"up": (0.49, 0.50, 300.0, 300.0)}
        signal_ts = None
    from polysignal.quant import Signal
    asyncio.run(ex.on_signal(St(), Signal("UP", 1.0, 0.08, 0.6, "t"), "PAPER"))
    row = store.conn.execute(
        "SELECT side, stake_usd, build_ms, ask_at_signal FROM shadow_execs").fetchone()
    assert row is not None
    side, stake, build_ms, ask = row
    assert side == "UP" and stake == 1.0 and ask == 0.50
    assert build_ms is not None and 0 < build_ms < 10_000

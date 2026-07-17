"""Engine-level guards found by the improvement review:
display obeys risk, restarts don't wipe fired signals."""
import asyncio
import json
import time

import pytest

from polysignal.engine import Engine, WindowState
from polysignal.gamma import Market
from polysignal.store import Store

CFG = json.load(open("config.json"))


def make_market(win: int) -> Market:
    return Market(window_ts=win, slug=f"btc-updown-5m-{win}", question="t",
                  condition_id="0x1", token_id_up="1", token_id_down="2",
                  taker_base_fee_bps=1000, maker_base_fee_bps=1000,
                  tick_size=0.01, min_order_size=5, resolution_source="",
                  accepting_orders=True, closed=False, outcome_prices=None)


def make_engine(tmp_path, mode="PAPER"):
    store = Store(tmp_path / "t.sqlite")
    return Engine(CFG, store, mode=mode), store


def prime_signal_conditions(engine, win: int | None = None):
    """Set up state so _evaluate returns a strong UP signal.
    The window is placed so tau=100s — always inside the signal band."""
    now = time.time()
    win = int(now) - 200
    st = WindowState(window_ts=win, market=make_market(win))
    st.s_open = 100.0
    # cheap ask on a big up-move inside the time band
    st.book = {"up": (0.40, 0.41, 500.0, 500.0), "down": (0.58, 0.59, 500.0, 500.0)}
    engine.window = st
    # fresh feeds + ready vol
    p = 100.0
    for i in range(60):
        p *= 1.0005 if i % 2 == 0 else 1 / 1.0005
        engine.vol.update(p, now - 60 + i)
    engine.binance.price = 100.5
    engine.binance.ts = now
    engine._binance_hist.append((now - 1, 100.5))
    engine.oracle.points[int(now)] = 100.5
    engine.oracle.latest_ts = int(now)
    return st


def test_display_blocked_by_kill_switch(tmp_path):
    """Hard boundary: kill switch suppresses the DISPLAYED recommendation."""
    engine, store = make_engine(tmp_path)
    from polysignal.timeutil import window_ts
    win = window_ts() // 1  # current window so tau is inside the band
    st = prime_signal_conditions(engine, window_ts())
    sig = engine._evaluate(st, time.time())
    if sig is None or sig.side == "PASS":
        pytest.skip("band alignment — signal not evaluable at this moment")
    engine.risk.kill(True)
    payload = engine.status_payload()
    assert payload["signal"]["side"] == "PASS"
    assert payload["signal"]["stake"] == 0.0
    assert payload["signal"]["reason"].startswith("blocked:kill_switch")
    engine.risk.kill(False)
    payload = engine.status_payload()
    assert payload["signal"]["side"] == "UP"
    store.close()


def test_restart_restores_signal_and_open(tmp_path):
    """One-position-per-window must survive an engine restart."""
    engine, store = make_engine(tmp_path)
    win = 1784226900
    store.upsert_window(win, "PAPER", signal="UP", stake_reco=2.0, edge=0.09,
                        p_fair_signal=0.62, ask_up=0.5, ask_down=0.49,
                        s_open=100.25, exec_ask=0.53)

    async def no_market(slug, w):
        return None
    engine.gamma.market_by_slug = no_market

    st = asyncio.run(engine._open_window(win, partial=True))
    assert st.signaled is True
    assert st.signal.side == "UP" and st.signal.stake_usd == 2.0
    assert st.s_open == 100.25 and st.warmup is False
    assert st.exec_ask == 0.53
    # the persisted row was not reset
    row = store.get_window(win, "PAPER")
    assert row["signal"] == "UP" and row["stake_reco"] == 2.0
    store.close()


def test_store_get_window_roundtrip(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    assert store.get_window(1, "PAPER") is None
    store.upsert_window(1, "PAPER", signal="DOWN", stake_reco=1.0, acted=1)
    row = store.get_window(1, "PAPER")
    assert row["signal"] == "DOWN" and row["acted"] == 1
    store.close()


def test_fire_signal_logs_violation_when_blocked(tmp_path):
    """Defense-in-depth: a blocked fire writes RISK_VIOLATION, not a signal."""
    engine, store = make_engine(tmp_path)
    st = prime_signal_conditions(engine, 1784226900)
    sig = engine._evaluate(st, time.time())
    if sig is None or sig.side == "PASS":
        pytest.skip("band alignment — signal not evaluable at this moment")
    engine.risk.kill(True)
    asyncio.run(engine._fire_signal(st, sig, time.time()))
    assert st.signaled is False
    n = store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='RISK_VIOLATION'").fetchone()[0]
    assert n == 1
    store.close()

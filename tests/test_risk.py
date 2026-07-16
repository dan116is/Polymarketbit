import time

import pytest

from polysignal.risk import RiskManager
from polysignal.store import Store

CFG = {
    "stake_ladder_usd": [1, 2, 5],
    "ladder_step_cents": 4.0,
    "daily_stop_usd": -10.0,
    "lock_hours": 24,
    "cooldown_windows_after_2_losses": 2,
    "max_positions_per_window": 1,
    "stake5_requires_gate2": True,
}


@pytest.fixture
def rm(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    yield RiskManager(store, CFG), store
    store.close()


def test_live_requires_green_gates(rm):
    r, store = rm
    assert not r.check("LIVE").allowed
    assert r.check("LIVE").reason == "gates_not_green"
    store.set_gate("GATE1", True)
    assert not r.check("LIVE").allowed
    store.set_gate("GATE2", True)
    assert r.check("LIVE").allowed


def test_daily_stop_locks_live_for_24h(rm):
    """DONE criterion: simulated breach of the daily loss cap locks LIVE 24h."""
    r, store = rm
    store.set_gate("GATE1", True)
    store.set_gate("GATE2", True)
    now = time.time()
    r.add_pnl(-4.0, now)
    assert r.check("LIVE", now).allowed
    r.add_pnl(-6.5, now)  # total -10.5 <= -10 -> lock
    v = r.check("LIVE", now)
    assert not v.allowed and v.reason.startswith("live_locked")
    # still locked 23h later, released after 24h
    assert not r.check("LIVE", now + 23 * 3600).allowed
    locked_until = float(store.get_state("live_locked_until"))
    assert locked_until == pytest.approx(now + 24 * 3600, abs=5)
    # PAPER continues to work while LIVE is locked
    assert r.check("PAPER", now).allowed


def test_two_losses_trigger_cooldown(rm):
    r, store = rm
    store.set_gate("GATE1", True)
    store.set_gate("GATE2", True)
    now = time.time()
    r.add_pnl(-1.0, now)
    assert r.check("LIVE", now).allowed
    r.add_pnl(-1.0, now)
    v = r.check("LIVE", now)
    assert not v.allowed and v.reason.startswith("cooldown")
    # a win resets the streak counter
    r.add_pnl(2.0, now)
    assert store.get_state("loss_streak") == "0"


def test_kill_switch_blocks_everything(rm):
    r, store = rm
    r.kill(True)
    assert not r.check("PAPER").allowed
    assert r.check("PAPER").reason == "kill_switch"
    r.kill(False)
    assert r.check("PAPER").allowed


def test_one_position_per_window(rm):
    r, _ = rm
    assert not r.check("PAPER", already_signaled_this_window=True).allowed

"""RISK — hard money rules. No model judgement, no overrides.

- LIVE recommendations require GATE1 and GATE2 green in the store. Period.
- Daily tracked loss <= daily_stop_usd (config, currently -$40) -> LIVE lock
  for 24h, persisted. (Raised from -$10 after the deep-improve analysis found
  the tighter stop fired ~60% of days and discarded ~half the +EV bets.)
- 2 consecutive tracked losses -> cooldown of N windows.
- One recommended position per window.
- Kill-switch flag (settable from Telegram) blocks everything until cleared.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .store import Store


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str


class RiskManager:
    def __init__(self, store: Store, cfg: dict):
        self.store = store
        self.daily_stop_usd = float(cfg["daily_stop_usd"])
        self.lock_hours = float(cfg["lock_hours"])
        self.cooldown_windows = int(cfg["cooldown_windows_after_2_losses"])
        self.stake5_requires_gate2 = bool(cfg.get("stake5_requires_gate2", True))

    # ---- state helpers -----------------------------------------------------
    def _today_key(self, now: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))

    def day_pnl(self, now: float | None = None) -> float:
        raw = self.store.get_state(f"day_pnl:{self._today_key(now)}", "0")
        return float(raw)

    def add_pnl(self, amount: float, now: float | None = None) -> None:
        key = f"day_pnl:{self._today_key(now)}"
        new = self.day_pnl(now) + amount
        self.store.set_state(key, str(new))
        if new <= self.daily_stop_usd:
            self.lock_live(f"daily stop hit: {new:+.2f} USD", now)
        # consecutive-loss tracking
        if amount < 0:
            streak = int(self.store.get_state("loss_streak", "0")) + 1
            self.store.set_state("loss_streak", str(streak))
            if streak >= 2:
                from .timeutil import window_ts, WINDOW_SECONDS
                until_ts = window_ts(now) + (1 + self.cooldown_windows) * WINDOW_SECONDS
                self.store.set_state("cooldown_until", str(until_ts))
                self.store.log_event("RISK_COOLDOWN",
                                     f"2 consecutive losses -> cooldown until {until_ts}")
        elif amount > 0:
            self.store.set_state("loss_streak", "0")

    def lock_live(self, reason: str, now: float | None = None) -> None:
        until = (now or time.time()) + self.lock_hours * 3600
        self.store.set_state("live_locked_until", str(until))
        self.store.log_event("RISK_LOCK", json.dumps({"reason": reason, "until": until}))

    def kill(self, on: bool) -> None:
        self.store.set_state("kill_switch", "1" if on else "0")
        self.store.log_event("KILL_SWITCH", "on" if on else "off")

    # ---- the verdict ---------------------------------------------------------
    def check(self, mode: str, now: float | None = None,
              already_signaled_this_window: bool = False) -> RiskVerdict:
        now = now or time.time()
        if self.store.get_state("kill_switch", "0") == "1":
            return RiskVerdict(False, "kill_switch")
        if already_signaled_this_window:
            return RiskVerdict(False, "one_position_per_window")
        if mode == "LIVE":
            if not (self.store.gate_green("GATE1") and self.store.gate_green("GATE2")):
                return RiskVerdict(False, "gates_not_green")
            locked_until = float(self.store.get_state("live_locked_until", "0"))
            if now < locked_until:
                return RiskVerdict(False, f"live_locked_until:{locked_until:.0f}")
            cooldown_until = float(self.store.get_state("cooldown_until", "0"))
            if now < cooldown_until:
                return RiskVerdict(False, f"cooldown_until:{cooldown_until:.0f}")
            if self.day_pnl(now) <= self.daily_stop_usd:
                self.lock_live("daily stop re-check", now)
                return RiskVerdict(False, "daily_stop")
        return RiskVerdict(True, "ok")

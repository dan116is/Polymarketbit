"""QUANT — deterministic fair value, EV and stake ladder. Pure functions, no I/O.

P_up = Phi( ln(S_t / S_open) / (sigma * sqrt(tau)) )

sigma is the per-second volatility of log returns (EWMA), tau is seconds
remaining, so sigma*sqrt(tau) is the stdev of the log move until close.
Tie rule (close >= open resolves Up) is absorbed by the continuous model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class EwmaVol:
    """EWMA variance of 1-second log returns, half-life in seconds.

    Feed it one price sample per second (uneven gaps are handled by scaling
    the squared return by 1/dt and decaying by dt seconds).
    """

    def __init__(self, halflife_s: float, sigma_floor: float = 1e-6):
        self.halflife_s = float(halflife_s)
        self.sigma_floor = float(sigma_floor)
        self._var: float | None = None
        self._last_price: float | None = None
        self._last_ts: float | None = None
        self.n_samples = 0

    def update(self, price: float, ts: float) -> None:
        if price <= 0:
            return
        if self._last_price is not None and self._last_ts is not None:
            dt = ts - self._last_ts
            if dt <= 0:
                return
            r = math.log(price / self._last_price)
            r2_per_s = (r * r) / dt  # variance contribution normalized to 1s
            decay = 0.5 ** (dt / self.halflife_s)
            if self._var is None:
                self._var = r2_per_s
            else:
                self._var = decay * self._var + (1.0 - decay) * r2_per_s
            self.n_samples += 1
        self._last_price = price
        self._last_ts = ts

    @property
    def sigma_1s(self) -> float:
        """Per-second stdev of log returns (floored)."""
        if self._var is None:
            return self.sigma_floor
        return max(math.sqrt(self._var), self.sigma_floor)

    def ready(self, min_samples: int = 30) -> bool:
        return self.n_samples >= min_samples


def p_up(s_t: float, s_open: float, sigma_1s: float, tau_s: float) -> float:
    """Probability the window resolves Up under driftless random walk."""
    if s_open <= 0 or s_t <= 0:
        raise ValueError("prices must be positive")
    if tau_s <= 0:
        return 1.0 if s_t >= s_open else 0.0
    denom = sigma_1s * math.sqrt(tau_s)
    if denom <= 0:
        return 1.0 if s_t >= s_open else 0.0
    return norm_cdf(math.log(s_t / s_open) / denom)


def taker_fee_per_share(price: float, taker_base_fee_bps: float) -> float:
    """Polymarket fee model: base_fee * min(p, 1-p) per share, base fee in bps.

    taker_base_fee_bps comes from the Gamma market object (`takerBaseFee`)
    at runtime — never hardcoded.
    """
    if not (0.0 < price < 1.0):
        return 0.0
    return (taker_base_fee_bps / 10_000.0) * min(price, 1.0 - price)


@dataclass
class Signal:
    side: str  # "UP" | "DOWN" | "PASS"
    stake_usd: float  # 0 for PASS
    edge: float  # net edge in probability units (dollars per share)
    p_fair: float
    reason: str


def stake_from_edge(net_edge: float, theta: float, ladder_step: float,
                    ladder: tuple[float, float, float] = (1.0, 2.0, 5.0),
                    gate2_green: bool = False) -> float:
    """Map net edge (after theta) to $1/$2/$5. $5 only after GATE 2."""
    if net_edge <= 0:
        return 0.0
    if net_edge <= ladder_step:
        return ladder[0]
    if net_edge <= 2 * ladder_step:
        return ladder[1]
    return ladder[2] if gate2_green else ladder[1]


def evaluate(
    *,
    s_t: float,
    s_open: float,
    sigma_1s: float,
    tau_s: float,
    ask_up: float | None,
    ask_down: float | None,
    taker_base_fee_bps: float,
    theta: float,
    buffer: float,
    band_s: tuple[float, float],
    ladder_step: float,
    gate2_green: bool = False,
    depth_up_usd: float = math.inf,
    depth_down_usd: float = math.inf,
) -> Signal:
    """Full signal decision for one snapshot. All amounts in probability units ($/share)."""
    pf = p_up(s_t, s_open, sigma_1s, tau_s)

    if not (band_s[0] <= tau_s <= band_s[1]):
        return Signal("PASS", 0.0, 0.0, pf, "outside_time_band")

    candidates = []
    if ask_up is not None and 0 < ask_up < 1:
        fee = taker_fee_per_share(ask_up, taker_base_fee_bps)
        edge = pf - ask_up - fee - buffer
        candidates.append(("UP", edge, ask_up, depth_up_usd))
    if ask_down is not None and 0 < ask_down < 1:
        fee = taker_fee_per_share(ask_down, taker_base_fee_bps)
        edge = (1.0 - pf) - ask_down - fee - buffer
        candidates.append(("DOWN", edge, ask_down, depth_down_usd))
    if not candidates:
        return Signal("PASS", 0.0, 0.0, pf, "no_quotes")

    side, edge, ask, depth = max(candidates, key=lambda c: c[1])
    if edge <= theta:
        return Signal("PASS", 0.0, edge, pf, "edge_below_theta")

    stake = stake_from_edge(edge - theta, theta, ladder_step, gate2_green=gate2_green)
    # Downgrade stake until the book can fill it at the quoted ask.
    while stake > 0 and stake > depth:
        stake = {5.0: 2.0, 2.0: 1.0, 1.0: 0.0}.get(stake, 0.0)
    if stake <= 0:
        return Signal("PASS", 0.0, edge, pf, "insufficient_depth")
    return Signal(side, stake, edge, pf, "edge_above_theta")

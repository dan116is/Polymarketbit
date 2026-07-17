"""M1 engine — per-window state machine tying feeds, quant, risk and delivery.

Spot estimate anchors on the oracle (resolution source) and extrapolates with
Binance movement since the last oracle point, so the Binance<->oracle basis
cancels instead of being guessed:

    S_est = oracle_latest + (binance_now - binance_at(oracle_latest_ts))

Cold start: the first partial window has no oracle open -> WARMUP, PASS only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .feeds import BinanceSpot, BookPoller, OracleFeed
from .gamma import GammaClient, Market
from .quant import EwmaVol, Signal, evaluate, taker_fee_per_share
from .risk import RiskManager
from .store import Store
from .timeutil import slug_for, t_remaining, window_ts

log = logging.getLogger("polysignal.engine")

SignalHook = Callable[[dict], Awaitable[None]]


@dataclass
class WindowState:
    window_ts: int
    market: Market | None = None
    s_open: float | None = None
    s_open_src_ts: int | None = None
    warmup: bool = False
    signaled: bool = False
    signal: Signal | None = None
    signal_ts: float | None = None
    signal_ask: float | None = None
    exec_ask: float | None = None
    book: dict = field(default_factory=dict)  # side -> (bid, ask, bdepth, adepth)
    outcome: str | None = None


class Engine:
    def __init__(self, cfg: dict, store: Store, mode: str = "PAPER",
                 hand_latency_s: float = 5.0):
        assert mode in ("PAPER", "LIVE")
        self.cfg = cfg
        self.mode = mode
        self.hand_latency_s = hand_latency_s
        self.store = store
        self.risk = RiskManager(store, cfg["risk"])
        rt = cfg["runtime"]
        self.gamma = GammaClient(rt["gamma_base"])
        self.books = BookPoller(rt["clob_base"])
        self.oracle = OracleFeed(rt["rtds_ws"])
        self.vol = EwmaVol(cfg["model"]["sigma_halflife_s"],
                           cfg["model"]["sigma_floor"])
        self._binance_hist: deque[tuple[float, float]] = deque(maxlen=1200)
        self.binance = BinanceSpot(rt["binance_ws_hosts"], on_price=self._on_binance)
        self.window: WindowState | None = None
        self.signal_hooks: list[SignalHook] = []
        self.status_hooks: list[SignalHook] = []
        self.alert_hooks: list[Callable[[str], Awaitable]] = []
        self._stop = asyncio.Event()
        self._last_vol_sample = 0.0
        self._last_heartbeat = 0.0
        self._feed_down_since: dict[str, float] = {}
        self._last_feed_alert = 0.0

    # ---- feed plumbing -----------------------------------------------------
    def _on_binance(self, price: float, ts: float) -> None:
        self._binance_hist.append((ts, price))
        # sample volatility at ~1s cadence, not per trade
        if ts - self._last_vol_sample >= 1.0:
            self.vol.update(price, ts)
            self._last_vol_sample = ts

    def _binance_at(self, ts: float) -> float | None:
        best = None
        for t, p in reversed(self._binance_hist):
            if t <= ts:
                best = p
                break
        return best if best is not None else (
            self._binance_hist[0][1] if self._binance_hist else None)

    def spot_estimate(self) -> float | None:
        """Oracle-anchored spot: basis cancels by construction."""
        if self.oracle.latest_ts == 0:
            return None
        anchor = self.oracle.points.get(self.oracle.latest_ts)
        if anchor is None:
            return None
        if int(time.time()) - self.oracle.latest_ts > 30:
            return None  # oracle stale beyond tolerance
        b_now = self.binance.price
        b_then = self._binance_at(self.oracle.latest_ts)
        if b_now is not None and b_then is not None:
            return anchor + (b_now - b_then)
        return anchor

    # ---- window lifecycle ----------------------------------------------------
    async def _open_window(self, win: int, partial: bool) -> WindowState:
        st = WindowState(window_ts=win, warmup=partial)
        rt = self.cfg["runtime"]
        slug = slug_for(win, rt["slug_prefix"])
        t0 = time.time()
        for _ in range(10):
            try:
                st.market = await self.gamma.market_by_slug(slug, win)
            except Exception as e:
                log.warning("gamma fetch failed: %r", e)
            if st.market:
                break
            await asyncio.sleep(0.5)
        if st.market:
            self.store.log_event("WINDOW_OPEN", json.dumps(
                {"window_ts": win, "slug": slug,
                 "detect_latency_s": round(time.time() - t0, 3),
                 "fee_bps": st.market.taker_base_fee_bps}))
        else:
            self.store.log_event("WINDOW_MISS", json.dumps({"window_ts": win, "slug": slug}))
            log.error("window %s: market not found — MISS", win)
        self.store.upsert_window(win, self.mode, signal=None, stake_reco=0.0)
        return st

    async def _try_capture_open(self, st: WindowState) -> None:
        if st.s_open is not None:
            return
        # Also attempted for the cold-start (warmup) window: the RTDS backlog
        # reaches ~70s back, so starting mid-window often still recovers S_open.
        got = self.oracle.price_at(st.window_ts, tolerance_s=3)
        if got:
            st.s_open, st.s_open_src_ts = got
            if st.warmup:
                st.warmup = False
                self.store.log_event("WARMUP_RECOVERED", json.dumps(
                    {"window_ts": st.window_ts, "s_open": st.s_open}))
            self.store.upsert_window(st.window_ts, self.mode,
                                     s_open=st.s_open,
                                     source_open="rtds_chainlink_btc_usd")

    async def _poll_books(self, st: WindowState) -> None:
        if not st.market:
            return
        bu, bd = await asyncio.gather(
            self.books.book(st.market.token_id_up),
            self.books.book(st.market.token_id_down))
        if bu:
            st.book["up"] = BookPoller.top(bu)
        if bd:
            st.book["down"] = BookPoller.top(bd)

    def _evaluate(self, st: WindowState, now: float) -> Signal | None:
        if st.warmup or st.s_open is None or not st.market:
            return None
        s_est = self.spot_estimate()
        if s_est is None or not self.vol.ready():
            return None
        m = self.cfg["model"]
        up = st.book.get("up")
        down = st.book.get("down")
        tau = st.window_ts + self.cfg["runtime"]["window_seconds"] - now
        gate2 = self.store.gate_green("GATE2")
        sig = evaluate(
            s_t=s_est, s_open=st.s_open, sigma_1s=self.vol.sigma_1s, tau_s=tau,
            ask_up=up[1] if up else None, ask_down=down[1] if down else None,
            taker_base_fee_bps=st.market.taker_base_fee_bps,
            theta=m["theta_cents"] / 100.0, buffer=m["buffer_cents"] / 100.0,
            band_s=tuple(m["signal_band_s"]),
            ladder_step=self.cfg["risk"]["ladder_step_cents"] / 100.0,
            gate2_green=gate2,
            depth_up_usd=up[3] if up else 0.0, depth_down_usd=down[3] if down else 0.0,
        )
        return sig

    async def _fire_signal(self, st: WindowState, sig: Signal, now: float) -> None:
        st.signaled = True
        st.signal = sig
        st.signal_ts = now
        side_book = st.book.get(sig.side.lower())
        st.signal_ask = side_book[1] if side_book else None
        up, down = st.book.get("up"), st.book.get("down")
        fee = taker_fee_per_share(st.signal_ask or 0.5,
                                  st.market.taker_base_fee_bps if st.market else 0.0)
        tau = st.window_ts + self.cfg["runtime"]["window_seconds"] - now
        self.store.upsert_window(
            st.window_ts, self.mode,
            p_fair_signal=sig.p_fair,
            ask_up=up[1] if up else None, ask_down=down[1] if down else None,
            bid_up=up[0] if up else None, bid_down=down[0] if down else None,
            fee=fee, edge=sig.edge, signal=sig.side, stake_reco=sig.stake_usd,
            t_remaining_sig=tau,
            latency_ms=(time.time() - now) * 1000.0,
        )
        self.store.log_event("SIGNAL", json.dumps(
            {"window_ts": st.window_ts, "side": sig.side, "stake": sig.stake_usd,
             "edge": round(sig.edge, 4), "p_fair": round(sig.p_fair, 4),
             "tau_s": round(tau, 1)}))
        payload = self.status_payload()
        for hook in self.signal_hooks:
            try:
                await hook(payload)
            except Exception as e:
                log.warning("signal hook failed: %r", e)
        # paper: sample the executable ask after simulated hand latency
        if self.mode == "PAPER" and st.market:
            asyncio.get_running_loop().create_task(self._sample_exec(st, sig))

    async def _sample_exec(self, st: WindowState, sig: Signal) -> None:
        await asyncio.sleep(self.hand_latency_s)
        token = (st.market.token_id_up if sig.side == "UP"
                 else st.market.token_id_down)
        b = await self.books.book(token)
        if b:
            _, ask, _, _ = BookPoller.top(b)
            st.exec_ask = ask
            self.store.upsert_window(st.window_ts, self.mode, exec_ask=ask)

    async def _close_window(self, st: WindowState) -> None:
        """Resolve outcome from the oracle open/close; log pnl for signals."""
        win_end = st.window_ts + self.cfg["runtime"]["window_seconds"]
        # oracle close: wait up to 30s for the boundary point
        s_close = None
        for _ in range(30):
            got = self.oracle.price_at(win_end, tolerance_s=3)
            if got:
                s_close = got[0]
                break
            await asyncio.sleep(1)
        outcome = None
        if s_close is not None and st.s_open is not None:
            outcome = "UP" if s_close >= st.s_open else "DOWN"
        st.outcome = outcome
        fields: dict = {"s_close": s_close, "outcome": outcome}
        if st.signal and st.signal.side in ("UP", "DOWN") and outcome:
            ask = st.exec_ask if st.exec_ask is not None else st.signal_ask
            if ask and 0 < ask < 1:
                stake = st.signal.stake_usd
                shares = stake / ask
                fee = taker_fee_per_share(ask, st.market.taker_base_fee_bps) * shares
                pnl = (shares * (1 - ask) - fee if st.signal.side == outcome
                       else -stake - fee)
                fields["pnl"] = round(pnl, 4)
                if self.mode in ("PAPER", "LIVE"):
                    self.risk.add_pnl(pnl)
        self.store.upsert_window(st.window_ts, self.mode, **fields)
        self.store.log_event("WINDOW_CLOSE", json.dumps(
            {"window_ts": st.window_ts, "outcome": outcome, "s_close": s_close}))

    # ---- health: heartbeat (uptime evidence) + feed-down alerts ---------------
    async def _health_check(self, now: float) -> None:
        feeds = {
            "binance": self.binance.price is not None and now - self.binance.ts < 10,
            "oracle": self.oracle.latest_ts > 0 and int(now) - self.oracle.latest_ts < 30,
        }
        if now - self._last_heartbeat >= 60:
            self._last_heartbeat = now
            self.store.log_event("HEARTBEAT", json.dumps(
                {"feeds": feeds, "window_ts": self.window.window_ts if self.window else None}))
        for name, ok in feeds.items():
            if ok:
                self._feed_down_since.pop(name, None)
            else:
                since = self._feed_down_since.setdefault(name, now)
                if now - since > 120 and now - self._last_feed_alert > 3600:
                    self._last_feed_alert = now
                    self.store.log_event("FEED_DOWN", name)
                    for hook in self.alert_hooks:
                        try:
                            await hook(f"⚠️ פיד {name} מנותק כבר יותר מ-2 דקות — "
                                       f"האיתותים מושהים עד שיתאושש")
                        except Exception as e:
                            log.warning("alert hook failed: %r", e)

    # ---- status for delivery/PWA ----------------------------------------------
    def status_payload(self) -> dict:
        st = self.window
        now = time.time()
        s_est = self.spot_estimate()
        payload = {
            "ts": now, "mode": self.mode,
            "gate1": self.store.gate_green("GATE1"),
            "gate2": self.store.gate_green("GATE2"),
            "day_pnl": self.risk.day_pnl(now),
            "basis": (self.binance.price - self.oracle.latest
                      if self.binance.price and self.oracle.latest else None),
            "feeds": {
                "binance": self.binance.price is not None and now - self.binance.ts < 10,
                "oracle": self.oracle.latest_ts > 0 and int(now) - self.oracle.latest_ts < 30,
            },
        }
        if st:
            tau = st.window_ts + self.cfg["runtime"]["window_seconds"] - now
            up, down = st.book.get("up"), st.book.get("down")
            sig = self._evaluate(st, now) if not st.signaled else st.signal
            payload.update({
                "window_ts": st.window_ts, "t_remaining": round(tau, 1),
                "warmup": st.warmup, "s_open": st.s_open, "s_est": s_est,
                "sigma_1s": self.vol.sigma_1s if self.vol.ready() else None,
                "ask_up": up[1] if up else None, "ask_down": down[1] if down else None,
                "signal": {
                    "side": sig.side if sig else "PASS",
                    "stake": sig.stake_usd if sig else 0.0,
                    "edge": round(sig.edge, 4) if sig else 0.0,
                    "p_fair": round(sig.p_fair, 4) if sig else None,
                    "reason": sig.reason if sig else "warming_up",
                    "locked": st.signaled,
                },
            })
        return payload

    # ---- main loop ----------------------------------------------------------------
    async def run(self) -> None:
        tasks = [asyncio.create_task(self.oracle.run()),
                 asyncio.create_task(self.binance.run())]
        first_win = window_ts()
        prev: WindowState | None = None
        try:
            while not self._stop.is_set():
                now = time.time()
                win = window_ts(now)
                if self.window is None or self.window.window_ts != win:
                    if self.window is not None:
                        prev = self.window
                        asyncio.get_running_loop().create_task(self._close_window(prev))
                    self.window = await self._open_window(win, partial=(win == first_win))
                st = self.window
                await self._try_capture_open(st)
                await self._poll_books(st)
                await self._health_check(now)
                # tick log (1 Hz)
                s_est = self.spot_estimate()
                up, down = st.book.get("up"), st.book.get("down")
                sig = None
                if not st.signaled:
                    sig = self._evaluate(st, now)
                    verdict = self.risk.check(self.mode, now,
                                              already_signaled_this_window=st.signaled)
                    if sig and sig.side != "PASS" and verdict.allowed:
                        await self._fire_signal(st, sig, now)
                    elif sig and sig.side != "PASS" and not verdict.allowed:
                        self.store.log_event("SIGNAL_BLOCKED", verdict.reason)
                self.store.log_tick(
                    ts=now, window_ts=win,
                    s_binance=self.binance.price,
                    s_oracle=self.oracle.latest,
                    basis=(self.binance.price - self.oracle.latest
                           if self.binance.price and self.oracle.latest else None),
                    ask_up=up[1] if up else None, ask_down=down[1] if down else None,
                    p_fair=sig.p_fair if sig else None,
                    sigma_1s=self.vol.sigma_1s if self.vol.ready() else None)
                for hook in self.status_hooks:
                    try:
                        await hook(self.status_payload())
                    except Exception as e:
                        log.warning("status hook failed: %r", e)
                await asyncio.sleep(max(0.0, 1.0 - (time.time() - now)))
        finally:
            for t in tasks:
                t.cancel()
            await self.gamma.close()
            await self.books.close()

    def stop(self) -> None:
        self._stop.set()

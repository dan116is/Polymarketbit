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

from .feeds import BinanceSpot, BookPoller, ClobBookWS, OracleFeed
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
    book_at: float = 0.0                      # last live book update (any source)
    official_outcome: str | None = None       # from CLOB market_resolved event
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
        self.clob_ws = ClobBookWS(rt["clob_ws"])
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
        self._feed_alerted: set[str] = set()
        self._last_feed_alert: dict[str, float] = {}
        self._last_market_retry = 0.0
        self._recent: deque[dict] = deque(maxlen=5)  # closed signals, for the PWA
        self._bg: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        """Background task with a strong reference — the event loop keeps only
        weak refs, so an unreferenced task can be GC'd mid-flight."""
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

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

        # A restart mid-window must NOT wipe what the previous process wrote:
        # restore s_open and any fired signal (one-position-per-window survives
        # restarts), and only create the row if it doesn't exist yet.
        row = self.store.get_window(win, self.mode)
        if row:
            if row.get("s_open") is not None:
                st.s_open = row["s_open"]
                st.warmup = False
                self.store.log_event("S_OPEN_RESTORED_FROM_DB", json.dumps(
                    {"window_ts": win, "s_open": st.s_open}))
            if row.get("signal") in ("UP", "DOWN"):
                st.signaled = True
                st.signal = Signal(row["signal"], row.get("stake_reco") or 0.0,
                                   row.get("edge") or 0.0,
                                   row.get("p_fair_signal") or 0.5, "restored")
                st.signal_ask = (row.get("ask_up") if row["signal"] == "UP"
                                 else row.get("ask_down"))
                st.exec_ask = row.get("exec_ask")
        else:
            self.store.upsert_window(win, self.mode, signal=None, stake_reco=0.0)

        # two quick attempts only — a Gamma outage must not freeze the 1Hz
        # loop; the main loop keeps retrying via _retry_market
        slug = slug_for(win, rt["slug_prefix"])
        for _ in range(2):
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
                 "detect_latency_s": round(time.time() - win, 3),
                 "fee_bps": st.market.taker_base_fee_bps}))
            self._start_clob_ws(st)
        return st

    def _start_clob_ws(self, st: WindowState) -> None:
        """Real-time books via the official market channel; runs 150s past
        the window close to catch the market_resolved (official outcome)."""
        keep_until = st.window_ts + self.cfg["runtime"]["window_seconds"] + 150
        self._spawn(self.clob_ws.run_window(st.market, st, keep_until))

    async def _retry_market(self, st: WindowState, now: float) -> None:
        """Keep re-fetching a missing market (rate-limited) instead of burning
        the whole window after a transient Gamma failure."""
        if st.market is not None or now - self._last_market_retry < 2.0:
            return
        self._last_market_retry = now
        slug = slug_for(st.window_ts, self.cfg["runtime"]["slug_prefix"])
        try:
            st.market = await self.gamma.market_by_slug(slug, st.window_ts)
        except Exception:
            return
        if st.market:
            self.store.log_event("WINDOW_OPEN", json.dumps(
                {"window_ts": st.window_ts, "slug": slug,
                 "detect_latency_s": round(now - st.window_ts, 3),
                 "fee_bps": st.market.taker_base_fee_bps, "late": True}))
            self._start_clob_ws(st)
        elif now - st.window_ts > 30 and not getattr(st, "_miss_logged", False):
            st._miss_logged = True
            self.store.log_event("WINDOW_MISS", json.dumps(
                {"window_ts": st.window_ts, "slug": slug}))
            log.error("window %s: market not found — MISS", st.window_ts)

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
        # REST fallback only — the CLOB websocket normally keeps the book
        # fresher than any polling could
        if not st.market or time.time() - st.book_at < 4:
            return
        bu, bd = await asyncio.gather(
            self.books.book(st.market.token_id_up),
            self.books.book(st.market.token_id_down))
        if bu:
            st.book["up"] = BookPoller.top(bu)
        if bd:
            st.book["down"] = BookPoller.top(bd)
        if bu or bd:
            st.book_at = time.time()

    def _evaluate(self, st: WindowState, now: float) -> Signal | None:
        if st.warmup or st.s_open is None or not st.market:
            return None
        s_est = self.spot_estimate()
        if s_est is None or not self.vol.ready():
            return None
        # a Binance outage freezes sigma at its last value — stale vol must
        # pause signals, not feed them
        if now - self.binance.ts > 15:
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
        # defense-in-depth: the caller already checked, but a signal must never
        # be written while risk blocks — if this trips, it's a real violation
        # and GATE 2 counts it
        verdict = self.risk.check(self.mode, now,
                                  already_signaled_this_window=st.signaled)
        if not verdict.allowed:
            self.store.log_event("RISK_VIOLATION", json.dumps(
                {"window_ts": st.window_ts, "reason": verdict.reason}))
            return
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
        # all modes: sample the executable ask over time (edge-decay evidence);
        # the hand-latency sample prices the pnl so LIVE isn't booked at the
        # optimistic 0s ask
        if st.market:
            self._spawn(self._sample_exec(st, sig))

    async def _sample_exec(self, st: WindowState, sig: Signal) -> None:
        """Edge-decay burst: the taken side's top-of-book at fixed offsets
        after the signal, persisted to exec_samples. This measured decay curve
        is the number a future M6 decision hinges on."""
        token = (st.market.token_id_up if sig.side == "UP"
                 else st.market.token_id_down)
        t0 = st.signal_ts or time.time()
        for dt in sorted({1.0, 2.0, 3.0, self.hand_latency_s, 8.0, 12.0}):
            delay = t0 + dt - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            b = await self.books.book(token)
            if not b:
                continue
            bid, ask, bdep, adep = BookPoller.top(b)
            self.store.log_exec_sample(st.window_ts, self.mode, dt,
                                       bid, ask, bdep, adep)
            if dt == self.hand_latency_s and ask and 0 < ask < 1:
                st.exec_ask = ask
                self.store.upsert_window(st.window_ts, self.mode, exec_ask=ask)

    async def _close_window(self, st: WindowState) -> None:
        """Resolve outcome from the oracle open/close; log pnl for signals."""
        win_end = st.window_ts + self.cfg["runtime"]["window_seconds"]
        # oracle close: wait up to 120s (the RTDS backlog depth) — a window
        # resolved on-chain must not silently drop out of risk accounting.
        # The CLOB market_resolved event (official outcome) can land earlier
        # or later; prefer it whenever it arrives.
        s_close = None
        for _ in range(120):
            if st.official_outcome:
                break
            got = self.oracle.price_at(win_end, tolerance_s=3)
            if got:
                s_close = got[0]
                break
            await asyncio.sleep(1)
        outcome = None
        if st.official_outcome:
            outcome = st.official_outcome
            self.store.log_event("OFFICIAL_RESOLUTION", json.dumps(
                {"window_ts": st.window_ts, "outcome": outcome}))
        elif s_close is not None and st.s_open is not None:
            outcome = "UP" if s_close >= st.s_open else "DOWN"
        elif st.signal and st.signal.side in ("UP", "DOWN"):
            self.store.log_event("UNRESOLVED_WINDOW", json.dumps(
                {"window_ts": st.window_ts,
                 "s_open": st.s_open, "s_close": s_close}))
            for hook in self.alert_hooks:
                try:
                    await hook(f"⚠️ חלון {st.window_ts} עם איתות נסגר בלי תוצאה "
                               f"מהאורקל — לא נכנס לחשבון הסיכון")
                except Exception as e:
                    log.warning("alert hook failed: %r", e)
        st.outcome = outcome
        fields: dict = {"s_close": s_close, "outcome": outcome}
        pnl = None
        if st.signal and st.signal.side in ("UP", "DOWN") and outcome:
            if st.exec_ask is None:
                self.store.log_event("EXEC_MISSING", json.dumps(
                    {"window_ts": st.window_ts}))
            ask = st.exec_ask if st.exec_ask is not None else st.signal_ask
            if ask and 0 < ask < 1 and st.market:
                stake = st.signal.stake_usd
                shares = stake / ask
                fee = taker_fee_per_share(ask, st.market.taker_base_fee_bps) * shares
                pnl = (shares * (1 - ask) - fee if st.signal.side == outcome
                       else -stake - fee)
                fields["pnl"] = round(pnl, 4)
                # PAPER tracks every recommendation (that IS the paper record);
                # in LIVE the -$10 stop must track what Daniel actually clicked
                # — hypothetical wins must not offset real losses
                row = self.store.get_window(st.window_ts, self.mode)
                acted = bool(row and row.get("acted"))
                if self.mode == "PAPER" or (self.mode == "LIVE" and acted):
                    # book to the window's own day (midnight-straddling windows)
                    self.risk.add_pnl(pnl, now=float(win_end))
        self.store.upsert_window(st.window_ts, self.mode, **fields)
        self.store.log_event("WINDOW_CLOSE", json.dumps(
            {"window_ts": st.window_ts, "outcome": outcome, "s_close": s_close}))
        # oracle-compare can differ from the official resolution exactly in the
        # tie/basis edge cases — keep listening and correct the record if so
        if outcome and not st.official_outcome and st.signal:
            self._spawn(self._verify_official(st, outcome, pnl))
        # close the loop for the user: outcome back to phone + PWA strip
        if st.signal and st.signal.side in ("UP", "DOWN") and outcome:
            won = st.signal.side == outcome
            self._recent.appendleft({
                "window_ts": st.window_ts, "side": st.signal.side,
                "stake": st.signal.stake_usd, "outcome": outcome,
                "pnl": round(pnl, 2) if pnl is not None else None, "won": won})
            msg = (f"{'✅' if won else '❌'} חלון {time.strftime('%H:%M', time.gmtime(st.window_ts))}: "
                   f"{st.signal.side} ${st.signal.stake_usd:.0f} → {outcome}"
                   + (f" | {pnl:+.2f}$" if pnl is not None else "")
                   + f" | סה\"כ היום {self.risk.day_pnl():+.2f}$ ({self.mode})")
            for hook in self.alert_hooks:
                try:
                    await hook(msg)
                except Exception as e:
                    log.warning("alert hook failed: %r", e)

    async def _verify_official(self, st: WindowState, oracle_outcome: str,
                               old_pnl: float | None) -> None:
        """If the official market_resolved event later disagrees with the
        oracle-compare outcome, correct the record and the risk accounting."""
        deadline = st.window_ts + self.cfg["runtime"]["window_seconds"] + 150
        while time.time() < deadline and not st.official_outcome:
            await asyncio.sleep(5)
        if not st.official_outcome or st.official_outcome == oracle_outcome:
            return
        outcome = st.official_outcome
        fields: dict = {"outcome": outcome}
        new_pnl = None
        if st.signal and st.signal.side in ("UP", "DOWN") and old_pnl is not None:
            ask = st.exec_ask if st.exec_ask is not None else st.signal_ask
            if ask and 0 < ask < 1 and st.market:
                stake = st.signal.stake_usd
                shares = stake / ask
                fee = taker_fee_per_share(ask, st.market.taker_base_fee_bps) * shares
                new_pnl = (shares * (1 - ask) - fee if st.signal.side == outcome
                           else -stake - fee)
                fields["pnl"] = round(new_pnl, 4)
                row = self.store.get_window(st.window_ts, self.mode)
                acted = bool(row and row.get("acted"))
                if self.mode == "PAPER" or (self.mode == "LIVE" and acted):
                    self.risk.add_pnl(new_pnl - old_pnl,
                                      now=float(st.window_ts + 300))
        self.store.upsert_window(st.window_ts, self.mode, **fields)
        self.store.log_event("OUTCOME_CORRECTED", json.dumps(
            {"window_ts": st.window_ts, "from": oracle_outcome, "to": outcome}))
        for hook in self.alert_hooks:
            try:
                await hook(f"⚠️ תיקון תוצאה לחלון "
                           f"{time.strftime('%H:%M', time.gmtime(st.window_ts))}: "
                           f"התוצאה הרשמית היא {outcome} (לא {oracle_outcome})")
            except Exception as e:
                log.warning("alert hook failed: %r", e)

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
                since = self._feed_down_since.pop(name, None)
                if name in self._feed_alerted:
                    # recovery: audit event with outage duration + phone update
                    self._feed_alerted.discard(name)
                    dur = round(now - since, 1) if since else None
                    self.store.log_event("FEED_UP", json.dumps(
                        {"feed": name, "outage_s": dur}))
                    for hook in self.alert_hooks:
                        try:
                            await hook(f"✅ פיד {name} חזר (היה מנותק "
                                       f"{dur or '?'} שניות) — האיתותים חודשו")
                        except Exception as e:
                            log.warning("alert hook failed: %r", e)
            else:
                since = self._feed_down_since.setdefault(name, now)
                if now - since > 120 and name not in self._feed_alerted:
                    # audit event once per outage, per feed; Telegram throttled
                    # per feed to one alert an hour
                    self._feed_alerted.add(name)
                    self.store.log_event("FEED_DOWN", name)
                    if now - self._last_feed_alert.get(name, 0.0) > 3600:
                        self._last_feed_alert[name] = now
                        for hook in self.alert_hooks:
                            try:
                                await hook(f"⚠️ פיד {name} מנותק כבר יותר מ-2 דקות "
                                           f"— האיתותים מושהים עד שיתאושש")
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
        payload["recent"] = list(self._recent)
        if st:
            tau = st.window_ts + self.cfg["runtime"]["window_seconds"] - now
            up, down = st.book.get("up"), st.book.get("down")
            sig = self._evaluate(st, now) if not st.signaled else st.signal
            # the DISPLAY obeys risk too — kill switch, locks, cooldown and red
            # gates suppress the recommendation itself, not only its logging
            blocked = None
            if sig and sig.side != "PASS" and not st.signaled:
                verdict = self.risk.check(self.mode, now)
                if not verdict.allowed:
                    blocked = verdict.reason
            payload.update({
                "window_ts": st.window_ts, "t_remaining": round(tau, 1),
                "warmup": st.warmup, "s_open": st.s_open, "s_est": s_est,
                "sigma_1s": self.vol.sigma_1s if self.vol.ready() else None,
                "ask_up": up[1] if up else None, "ask_down": down[1] if down else None,
                "signal": {
                    "side": "PASS" if blocked else (sig.side if sig else "PASS"),
                    "stake": 0.0 if blocked else (sig.stake_usd if sig else 0.0),
                    "edge": round(sig.edge, 4) if sig else 0.0,
                    "p_fair": round(sig.p_fair, 4) if sig else None,
                    "reason": (f"blocked:{blocked}" if blocked
                               else (sig.reason if sig else "warming_up")),
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
                        self._spawn(self._close_window(prev))
                    self.window = await self._open_window(win, partial=(win == first_win))
                st = self.window
                await self._retry_market(st, now)
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
            # let in-flight window-close/exec-sample tasks finish BEFORE the
            # caller closes the store — otherwise a stop near a boundary loses
            # that window's outcome or writes to a closed DB
            if self._bg:
                await asyncio.wait(self._bg, timeout=35)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.gamma.close()
            await self.books.close()

    def stop(self) -> None:
        self._stop.set()

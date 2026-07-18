"""M6 — execution pipeline. Two halves, one hard boundary.

ShadowExecutor (runs now, zero risk): on every signal it performs the FULL
order preparation — real EIP-712 signing via py-clob-client with a THROWAWAY
key — and stops before sending. It records the true signal->order-ready
latency and the live ask drift over it. Those two numbers decide the real bot.

LiveExecutor (built, DISARMED): the same pipeline plus post_order, refused
unless ALL of: config m6.armed set manually by Daniel, GATE1B and GATE2B green
in the store, credentials present in the environment, and RISK allows. There
is no code path that trades without every one of these.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

log = logging.getLogger("polysignal.executor")


MARKETABLE_CUSHION = 0.02


def marketable_price(ask: float, cushion: float = MARKETABLE_CUSHION) -> float:
    """Aggressive marketable BUY limit: cross the ask by a cushion, capped at
    0.99. Deliberately NOT capped at the signal-time ask — the deep-improve
    audit showed that capping there collapses test EV from +2.66c to +0.18c,
    because it kills exactly the adverse-ask-up fills that are the market
    confirming our signal (those fills carry +1.92c EV each). A regression
    test locks this in."""
    return min(0.99, ask + cushion)


def _make_clob_client(private_key: str, funder: str | None = None):
    """py-clob-client wired for order building; L1-only (no API creds needed
    for building/signing)."""
    from py_clob_client.client import ClobClient
    kwargs = {"key": private_key, "chain_id": 137}
    if funder:
        kwargs.update({"signature_type": 1, "funder": funder})
    return ClobClient("https://clob.polymarket.com", **kwargs)


def _build_signed_order(client, token_id: str, price: float, stake_usd: float,
                        tick_size: float, neg_risk: bool):
    """Build+sign a marketable limit BUY — the real cryptographic work whose
    duration we are measuring. Uses the client's local order builder directly:
    ClobClient.create_order silently calls the API when neg_risk is falsy,
    which would poison the latency measurement (and fail on throwaway keys)."""
    from py_clob_client.clob_types import CreateOrderOptions, OrderArgs
    from py_clob_client.order_builder.constants import BUY
    size = round(stake_usd / price, 2)
    args = OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
    return client.builder.create_order(args, CreateOrderOptions(
        tick_size=str(tick_size), neg_risk=neg_risk))


class ShadowExecutor:
    """Measures what the real bot would experience — without trading."""

    # measured warm POST round-trip to clob.polymarket.com (2026-07-17):
    # ~150ms warm, so the estimated real latency = input staleness + build +
    # this. Overridable via config m6.warm_post_ms.
    DEFAULT_WARM_POST_MS = 150.0

    def __init__(self, store, cfg: dict):
        self.store = store
        self.cfg = cfg
        self._client = None
        self.warm_post_ms = float(cfg.get("m6", {}).get(
            "warm_post_ms", self.DEFAULT_WARM_POST_MS))
        # delay before the tail-fill probe; 1s live, set to 0 in tests
        self.tail_probe_s = float(cfg.get("m6", {}).get("tail_probe_s", 1.0))

    def _ensure_client(self):
        if self._client is None:
            from eth_account import Account
            throwaway = Account.create().key.hex()  # not a real wallet
            self._client = _make_clob_client(throwaway)
        return self._client

    async def warmup(self) -> None:
        """Pre-build one dummy order at startup: client construction and
        module imports cost ~1.2s the FIRST time — the real bot pre-warms the
        same way, so measurements must reflect steady state, not cold start."""
        try:
            client = await asyncio.to_thread(self._ensure_client)
            await asyncio.to_thread(
                _build_signed_order, client, "1" * 70, 0.5, 1.0, 0.01, False)
            log.info("shadow executor warmed up")
        except Exception as e:
            log.warning("shadow warmup failed: %r", e)

    async def on_signal(self, st, sig, mode: str) -> None:
        """st: engine WindowState with a live CLOB-WS book; sig: quant.Signal."""
        if not st.market or sig.side not in ("UP", "DOWN"):
            return
        side_key = sig.side.lower()
        book = st.book.get(side_key)
        ask_at_signal = book[1] if book else None
        token = (st.market.token_id_up if sig.side == "UP"
                 else st.market.token_id_down)
        t0 = time.perf_counter()
        try:
            client = self._ensure_client()
            price = marketable_price(ask_at_signal or 0.5)
            await asyncio.to_thread(
                _build_signed_order, client, token, price, sig.stake_usd,
                st.market.tick_size, False)
            build_ms = (time.perf_counter() - t0) * 1000
        except Exception as e:
            log.warning("shadow build failed: %r", e)
            build_ms = None
        book2 = st.book.get(side_key)
        ask_at_ready = book2[1] if book2 else None
        drift = ((ask_at_ready - ask_at_signal) * 100
                 if ask_at_ready is not None and ask_at_signal is not None else None)
        # THE real capturable latency: how stale the inputs were when we decided
        # + the crypto build + the warm POST. build_ms alone hid the first term.
        input_age_ms = getattr(st, "input_age_ms", None)
        est_roundtrip_ms = (
            (input_age_ms or 0.0) + (build_ms or 0.0) + self.warm_post_ms
            if build_ms is not None else None)

        # tail-fill probe: 1s after the signal, would a marketable order at
        # (signal ask + 2c cushion) STILL clear? 64% of backtest return comes
        # from fast-move windows where the ask gaps exactly then — this is the
        # single riskiest live assumption, measured directly.
        cushion = 0.02
        await asyncio.sleep(self.tail_probe_s)
        book3 = st.book.get(side_key)
        ask_1s = book3[1] if book3 else None
        ask_drift_1s = ((ask_1s - ask_at_signal) * 100
                        if ask_1s is not None and ask_at_signal is not None else None)
        would_fill = (1 if (ask_1s is not None and ask_at_signal is not None
                            and ask_1s <= ask_at_signal + cushion) else 0)

        self.store.conn.execute(
            "INSERT OR REPLACE INTO shadow_execs "
            "(window_ts, mode, side, stake_usd, t_signal, build_ms, "
            " ask_at_signal, ask_at_ready, drift_cents, input_age_ms, "
            " est_roundtrip_ms, ask_drift_1s_cents, would_fill) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (st.window_ts, mode, sig.side, sig.stake_usd, st.signal_ts or time.time(),
             build_ms, ask_at_signal, ask_at_ready, drift, input_age_ms,
             est_roundtrip_ms, ask_drift_1s, would_fill))
        self.store.conn.commit()
        self.store.log_event("SHADOW_EXEC", json.dumps(
            {"window_ts": st.window_ts, "build_ms": build_ms,
             "input_age_ms": round(input_age_ms, 1) if input_age_ms else None,
             "est_roundtrip_ms": round(est_roundtrip_ms, 1) if est_roundtrip_ms else None,
             "ask_drift_1s_cents": ask_drift_1s, "would_fill": would_fill}))


class LiveExecutor:
    """The real thing — and it refuses to exist until every lock opens."""

    REFUSALS = ("not_armed", "gate1b_red", "gate2b_red", "no_credentials",
                "risk_blocked")

    def __init__(self, store, risk, cfg: dict):
        self.store = store
        self.risk = risk
        self.cfg = cfg
        self._client = None

    async def keep_warm(self) -> None:
        """One cheap GET on the SAME httpx pool post_order uses. Measured
        2026-07-17: a cold connection costs 650-950ms vs ~150ms warm, and
        httpx's default keepalive expiry is 5s — so the engine pings every
        ~3s inside the action band, or every live order pays cold TLS."""
        try:
            from py_clob_client.http_helpers.helpers import _http_client
            await asyncio.to_thread(
                _http_client.get, "https://clob.polymarket.com/ok")
        except Exception:
            pass  # warmth is best-effort; never let it disturb the engine

    def refusal(self, mode: str) -> str | None:
        """Why a live order may NOT be sent right now; None = all clear."""
        m6 = self.cfg.get("m6", {})
        if not m6.get("armed", False):
            return "not_armed"
        if not self.store.gate_green("GATE1B"):
            return "gate1b_red"
        if not self.store.gate_green("GATE2B"):
            return "gate2b_red"
        if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
            return "no_credentials"
        verdict = self.risk.check("LIVE")
        if not verdict.allowed:
            return "risk_blocked"
        return None

    async def fire(self, st, sig, mode: str) -> dict:
        why = self.refusal(mode)
        if why is not None:
            self.store.log_event("LIVE_BOT_REFUSED", json.dumps(
                {"window_ts": st.window_ts, "reason": why}))
            return {"sent": False, "reason": why}
        # book sanity BEFORE any client/network work: with the CLOB WS down
        # the REST fallback refreshes only every ~4s — never cross an ask that
        # old, the edge model priced a book that no longer exists
        book = st.book.get(sig.side.lower())
        ask = book[1] if book else None
        if ask is None or not (0 < ask < 1):
            return {"sent": False, "reason": "no_book"}
        if time.time() - (getattr(st, "book_at", 0.0) or 0.0) > 2.0:
            self.store.log_event("LIVE_BOT_REFUSED", json.dumps(
                {"window_ts": st.window_ts, "reason": "stale_book"}))
            return {"sent": False, "reason": "stale_book"}
        # every lock is open — build and send a real (tiny) order
        if self._client is None:
            self._client = _make_clob_client(
                os.environ["POLYMARKET_PRIVATE_KEY"],
                os.environ.get("POLYMARKET_FUNDER") or None)
            creds = await asyncio.to_thread(self._client.create_or_derive_api_creds)
            self._client.set_api_creds(creds)
        token = (st.market.token_id_up if sig.side == "UP"
                 else st.market.token_id_down)
        stake = min(sig.stake_usd, float(self.cfg["m6"].get("max_stake_usd", 1.0)))
        price = marketable_price(ask)
        order = await asyncio.to_thread(
            _build_signed_order, self._client, token, price, stake,
            st.market.tick_size, False)
        from py_clob_client.clob_types import OrderType
        resp = await asyncio.to_thread(self._client.post_order, order, OrderType.FAK)
        self.store.log_event("LIVE_BOT_ORDER", json.dumps(
            {"window_ts": st.window_ts, "side": sig.side, "stake": stake,
             "price": price, "resp": str(resp)[:300]}))
        return {"sent": True, "resp": resp}

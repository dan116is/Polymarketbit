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

    def __init__(self, store, cfg: dict):
        self.store = store
        self.cfg = cfg
        self._client = None

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
            # marketable price: cross the current ask (capped to book bounds)
            price = min(0.99, (ask_at_signal or 0.5) + 0.02)
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
        self.store.conn.execute(
            "INSERT OR REPLACE INTO shadow_execs VALUES (?,?,?,?,?,?,?,?,?)",
            (st.window_ts, mode, sig.side, sig.stake_usd, st.signal_ts or time.time(),
             build_ms, ask_at_signal, ask_at_ready, drift))
        self.store.conn.commit()
        self.store.log_event("SHADOW_EXEC", json.dumps(
            {"window_ts": st.window_ts, "build_ms": build_ms,
             "ask_signal": ask_at_signal, "ask_ready": ask_at_ready}))


class LiveExecutor:
    """The real thing — and it refuses to exist until every lock opens."""

    REFUSALS = ("not_armed", "gate1b_red", "gate2b_red", "no_credentials",
                "risk_blocked")

    def __init__(self, store, risk, cfg: dict):
        self.store = store
        self.risk = risk
        self.cfg = cfg
        self._client = None

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
        # every lock is open — build and send a real (tiny) order
        if self._client is None:
            self._client = _make_clob_client(
                os.environ["POLYMARKET_PRIVATE_KEY"],
                os.environ.get("POLYMARKET_FUNDER") or None)
            creds = await asyncio.to_thread(self._client.create_or_derive_api_creds)
            self._client.set_api_creds(creds)
        token = (st.market.token_id_up if sig.side == "UP"
                 else st.market.token_id_down)
        book = st.book.get(sig.side.lower())
        ask = book[1] if book else None
        if ask is None or not (0 < ask < 1):
            return {"sent": False, "reason": "no_book"}
        stake = min(sig.stake_usd, float(self.cfg["m6"].get("max_stake_usd", 1.0)))
        price = min(0.99, ask + 0.02)  # marketable cap
        order = await asyncio.to_thread(
            _build_signed_order, self._client, token, price, stake,
            st.market.tick_size, False)
        from py_clob_client.clob_types import OrderType
        resp = await asyncio.to_thread(self._client.post_order, order, OrderType.FAK)
        self.store.log_event("LIVE_BOT_ORDER", json.dumps(
            {"window_ts": st.window_ts, "side": sig.side, "stake": stake,
             "price": price, "resp": str(resp)[:300]}))
        return {"sent": True, "resp": resp}

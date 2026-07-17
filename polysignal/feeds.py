"""WATCHER feeds — Binance spot WS, Polymarket RTDS (Chainlink oracle), CLOB books.

All clients auto-reconnect with exponential backoff. Binance is a low-latency
*proxy*; the oracle feed (RTDS crypto_prices_chainlink, symbol btc/usd — the
market's actual resolution source) provides S_open at window boundaries and a
live basis measurement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import aiohttp
import websockets

log = logging.getLogger("polysignal.feeds")


async def _reconnect_loop(name: str, connect_once, stop: asyncio.Event):
    backoff = 1.0
    while not stop.is_set():
        try:
            await connect_once()
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s disconnected: %r — reconnect in %.0fs", name, e, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)


class BinanceSpot:
    """btcusdt trade stream; keeps latest price and calls on_price(price, ts)."""

    def __init__(self, hosts: list[str], on_price: Callable[[float, float], None]):
        self.hosts = hosts
        self.on_price = on_price
        self.price: float | None = None
        self.ts: float = 0.0
        self._host_i = 0
        self.stop = asyncio.Event()

    async def _connect_once(self):
        url = f"{self.hosts[self._host_i % len(self.hosts)]}/btcusdt@trade"
        self._host_i += 1
        async with websockets.connect(url, open_timeout=10, ping_interval=20) as ws:
            log.info("binance connected: %s", url)
            got_data = False
            while True:
                # stall watchdog: BTCUSDT never goes 30s without a trade; a
                # silent-but-open socket must be treated as dead
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    raise ConnectionError("binance stall: no trade for 30s")
                d = json.loads(msg)
                if not got_data:
                    # only a host that actually DELIVERS counts as good — a
                    # handshake-only host must not stop the rotation
                    got_data = True
                    self._host_i -= 1
                p = float(d["p"])
                ts = d["T"] / 1000.0
                self.price, self.ts = p, ts
                self.on_price(p, ts)

    async def run(self):
        await _reconnect_loop("binance", self._connect_once, self.stop)


class OracleFeed:
    """Polymarket RTDS — Chainlink btc/usd stream (the resolution source).

    Server sends a ~2min second-resolution backlog on subscribe and then
    incremental updates. We keep a rolling ts->price map so the exact price at
    a window boundary (S_open) can be read even if it arrives a moment late.
    Application-level "ping" frames must be answered with "pong".
    """

    def __init__(self, url: str, on_point: Callable[[int, float], None] | None = None,
                 resubscribe_s: float = 10.0, keep_s: int = 900):
        self.url = url
        self.on_point = on_point
        self.resubscribe_s = resubscribe_s
        self.keep_s = keep_s
        self.points: dict[int, float] = {}  # unix_seconds -> oracle price
        self.latest_ts: int = 0
        self.stop = asyncio.Event()

    @property
    def latest(self) -> float | None:
        return self.points.get(self.latest_ts)

    def price_at(self, ts: int, tolerance_s: int = 3) -> tuple[float, int] | None:
        """Oracle price at `ts`, or the nearest point within +-tolerance
        (earlier preferred). The later side matters after reconnects, when the
        backlog may start just past a window boundary."""
        if ts in self.points:
            return self.points[ts], ts
        for d in range(1, tolerance_s + 1):
            if (ts - d) in self.points:
                return self.points[ts - d], ts - d
            if (ts + d) in self.points:
                return self.points[ts + d], ts + d
        return None

    def _ingest(self, payload: dict):
        data = payload.get("data")
        pts = data if isinstance(data, list) else [payload]
        for d in pts:
            if not isinstance(d, dict):
                continue
            ts_ms, v = d.get("timestamp"), d.get("value")
            if ts_ms is None or v is None:
                continue
            ts = int(ts_ms // 1000)
            self.points[ts] = float(v)
            if ts > self.latest_ts:
                self.latest_ts = ts
            if self.on_point:
                self.on_point(ts, float(v))
        # prune
        cutoff = self.latest_ts - self.keep_s
        for k in [k for k in self.points if k < cutoff]:
            del self.points[k]

    async def _connect_once(self):
        sub = json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*",
             "filters": json.dumps({"symbol": "btc/usd"})},
        ]})
        async with websockets.connect(self.url, open_timeout=10) as ws:
            log.info("oracle RTDS connected")
            await ws.send(sub)
            last_sub = time.time()
            conn_started = time.time()
            while not self.stop.is_set():
                # stall watchdog FIRST, so it also runs on the recv-timeout and
                # ping paths (an open-but-silent subscription is dead) and on a
                # connection that never delivered a single point
                if time.time() - max(self.latest_ts, conn_started) > 60:
                    raise ConnectionError("oracle stall: no new point for 60s")
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    # periodic re-subscribe: server re-sends the backlog, which
                    # both refreshes data and acts as a liveness check
                    if time.time() - last_sub >= self.resubscribe_s:
                        await ws.send(sub)
                        last_sub = time.time()
                    continue
                if isinstance(msg, str) and msg.strip().lower() == "ping":
                    await ws.send("pong")
                    continue
                try:
                    m = json.loads(msg)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = m.get("payload")
                if isinstance(payload, dict) and payload.get("symbol") == "btc/usd":
                    self._ingest(payload)
                if time.time() - last_sub >= self.resubscribe_s:
                    await ws.send(sub)
                    last_sub = time.time()

    async def run(self):
        await _reconnect_loop("oracle", self._connect_once, self.stop)


class BookPoller:
    """CLOB REST /book poller for the active window's two tokens (1 Hz).

    Simple and robust for a 15-120s signal band; the CLOB WS can replace this
    for the M6 bot where milliseconds matter.
    """

    def __init__(self, base: str, session: aiohttp.ClientSession | None = None):
        self.base = base.rstrip("/")
        self._session = session

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5))
        return self._session

    @staticmethod
    def top(book: dict) -> tuple[float | None, float | None, float, float]:
        """(best_bid, best_ask, bid_depth_usd, ask_depth_usd) at top of book."""
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        # CLOB returns levels sorted away from the touch; take max bid / min ask.
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        bid_depth = sum(float(b["size"]) * float(b["price"]) for b in bids
                        if best_bid is not None and float(b["price"]) == best_bid)
        ask_depth = sum(float(a["size"]) * float(a["price"]) for a in asks
                        if best_ask is not None and float(a["price"]) == best_ask)
        return best_bid, best_ask, bid_depth, ask_depth

    async def book(self, token_id: str) -> dict | None:
        s = await self._ensure()
        try:
            async with s.get(f"{self.base}/book", params={"token_id": token_id}) as r:
                if r.status != 200:
                    return None
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

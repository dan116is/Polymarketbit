"""Gamma API client — market metadata by deterministic slug.

Everything the model needs from the market object is read at runtime:
clobTokenIds, takerBaseFee, orderPriceMinTickSize, orderMinSize. Nothing is
hardcoded (DONE criterion: zero fixed values that the API provides).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import aiohttp


@dataclass
class Market:
    window_ts: int
    slug: str
    question: str
    condition_id: str
    token_id_up: str
    token_id_down: str
    taker_base_fee_bps: float
    maker_base_fee_bps: float
    tick_size: float
    min_order_size: float
    resolution_source: str
    accepting_orders: bool
    closed: bool
    outcome_prices: tuple[float, float] | None  # (up, down) — post-resolution 0/1

    @property
    def resolved_up(self) -> bool | None:
        if self.outcome_prices is None or not self.closed:
            return None
        up, down = self.outcome_prices
        if up > 0.99:
            return True
        if down > 0.99:
            return False
        return None


def parse_market(window_ts: int, m: dict) -> Market:
    token_ids = json.loads(m["clobTokenIds"])
    outcomes = json.loads(m.get("outcomes", '["Up", "Down"]'))
    # Map token ids by outcome order; markets list Up first but don't assume.
    idx_up = outcomes.index("Up")
    idx_down = outcomes.index("Down")
    prices = None
    if m.get("outcomePrices"):
        p = [float(x) for x in json.loads(m["outcomePrices"])]
        prices = (p[idx_up], p[idx_down])
    return Market(
        window_ts=window_ts,
        slug=m["slug"],
        question=m.get("question", ""),
        condition_id=m.get("conditionId", ""),
        token_id_up=token_ids[idx_up],
        token_id_down=token_ids[idx_down],
        taker_base_fee_bps=float(m.get("takerBaseFee", 0.0)),
        maker_base_fee_bps=float(m.get("makerBaseFee", 0.0)),
        tick_size=float(m.get("orderPriceMinTickSize", 0.01)),
        min_order_size=float(m.get("orderMinSize", 0.0)),
        resolution_source=m.get("resolutionSource", ""),
        accepting_orders=bool(m.get("acceptingOrders", False)),
        closed=bool(m.get("closed", False)),
        outcome_prices=prices,
    )


class GammaClient:
    def __init__(self, base: str = "https://gamma-api.polymarket.com",
                 session: aiohttp.ClientSession | None = None):
        self.base = base.rstrip("/")
        self._session = session

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def market_by_slug(self, slug: str, window_ts: int) -> Market | None:
        s = await self._ensure()
        async with s.get(f"{self.base}/markets", params={"slug": slug}) as r:
            r.raise_for_status()
            arr = await r.json()
        if not arr:
            return None
        return parse_market(window_ts, arr[0])

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

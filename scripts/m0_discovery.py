"""M0 — Discovery Spike.

Prints, live: active market (deterministic slug) -> tokenIds -> live book ->
window OPEN PRICE from the resolution source (RTDS Chainlink btc/usd feed).

DONE criteria (plan §4/M0): runs 30 min, detects every new window <=2s from
open, prints open+book per window; the open-price-source question is closed.

Usage: python -m scripts.m0_discovery [minutes]
Evidence log goes to stdout — redirect to reports/m0_discovery.log.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time

sys.path.insert(0, ".")

from polysignal.feeds import BinanceSpot, BookPoller, OracleFeed
from polysignal.gamma import GammaClient
from polysignal.timeutil import WINDOW_SECONDS, slug_for, window_ts

CFG = json.load(open("config.json"))
RT = CFG["runtime"]


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


async def main(run_minutes: float = 30.0):
    oracle = OracleFeed(RT["rtds_ws"])
    binance = BinanceSpot(RT["binance_ws_hosts"], on_price=lambda p, ts: None)
    gamma = GammaClient(RT["gamma_base"])
    books = BookPoller(RT["clob_base"])

    feed_tasks = [asyncio.create_task(oracle.run()),
                  asyncio.create_task(binance.run())]

    t_end = time.time() + run_minutes * 60
    results = []
    basis_samples = []
    log(f"M0 discovery spike — running {run_minutes:.0f} minutes")

    # Sample the basis (binance - oracle) once per second in the background.
    async def basis_sampler():
        while time.time() < t_end:
            if binance.price is not None and oracle.latest is not None \
                    and oracle.latest_ts >= int(time.time()) - 15:
                basis_samples.append(binance.price - oracle.points[oracle.latest_ts])
            await asyncio.sleep(1)
    feed_tasks.append(asyncio.create_task(basis_sampler()))

    seen: set[int] = set()
    first_win = window_ts()  # started mid-window: partial, excluded from stats
    while time.time() < t_end:
        now = time.time()
        win = window_ts(now)
        if win in seen:
            await asyncio.sleep(0.05)
            continue
        seen.add(win)
        boundary = float(win)
        rec: dict = {"window_ts": win, "slug": slug_for(win, RT["slug_prefix"]),
                     "partial": win == first_win}

        # 1. Deterministic identification via Gamma
        t0 = time.time()
        mkt = None
        for attempt in range(10):
            try:
                mkt = await gamma.market_by_slug(rec["slug"], win)
            except Exception as e:
                log(f"gamma error: {e!r}")
            if mkt:
                break
            await asyncio.sleep(0.5)
        rec["gamma_latency_s"] = round(time.time() - t0, 3)
        rec["detect_latency_s"] = round(time.time() - boundary, 3)
        if not mkt:
            log(f"window {win}: MARKET NOT FOUND after retries — MISS")
            rec["miss"] = True
            results.append(rec)
            continue
        rec.update(question=mkt.question,
                   taker_fee_bps=mkt.taker_base_fee_bps,
                   tick_size=mkt.tick_size, min_order=mkt.min_order_size,
                   token_up=mkt.token_id_up[:16] + "...",
                   token_down=mkt.token_id_down[:16] + "...")
        log(f"window {win} [{rec['slug']}] identified in {rec['detect_latency_s']}s "
            f"| fee={mkt.taker_base_fee_bps}bps tick={mkt.tick_size} minOrder={mkt.min_order_size}")

        # 2. Live book snapshot
        bu, bd = await asyncio.gather(books.book(mkt.token_id_up),
                                      books.book(mkt.token_id_down))
        for name, b in (("up", bu), ("down", bd)):
            if b:
                bid, ask, bdep, adep = BookPoller.top(b)
                rec[f"book_{name}"] = {"bid": bid, "ask": ask,
                                       "bid_depth_usd": round(bdep, 2),
                                       "ask_depth_usd": round(adep, 2)}
        log(f"window {win} book: up={rec.get('book_up')} down={rec.get('book_down')}")

        # 3. Open price from the resolution source (oracle feed).
        # A partial (cold-start) window predates the ~70s RTDS backlog — the
        # engine policy for that case is WARMUP/PASS, so skip the wait.
        if rec["partial"]:
            log(f"window {win}: partial cold-start window — S_open n/a (WARMUP)")
            results.append(rec)
            continue
        t0 = time.time()
        s_open = None
        while time.time() - boundary < 45:
            got = oracle.price_at(win, tolerance_s=2)
            if got:
                s_open, src_ts = got
                rec["s_open"] = s_open
                rec["s_open_src_ts"] = src_ts
                rec["s_open_capture_s"] = round(time.time() - boundary, 3)
                break
            await asyncio.sleep(0.5)
        if s_open is None:
            log(f"window {win}: ORACLE OPEN NOT CAPTURED within 45s ⚠")
            rec["open_missing"] = True
        else:
            b = binance.price
            rec["s_binance_now"] = b
            log(f"window {win} S_open(oracle)={s_open:.2f} captured "
                f"+{rec['s_open_capture_s']}s | binance_now={b}")
        results.append(rec)

    # ---- summary -------------------------------------------------------------
    for t in feed_tasks:
        t.cancel()
    await gamma.close()
    await books.close()

    full = [r for r in results if not r.get("miss") and not r.get("partial")]
    det = [r["detect_latency_s"] for r in full]
    caps = [r["s_open_capture_s"] for r in full if "s_open_capture_s" in r]
    log("=" * 70)
    log(f"SUMMARY windows_seen={len(results)} identified={len(full)} "
        f"misses={sum(1 for r in results if r.get('miss'))} "
        f"open_captured={len(caps)}")
    if det:
        log(f"detect latency s: max={max(det)} mean={statistics.mean(det):.3f}")
    if caps:
        log(f"S_open capture s: max={max(caps)} mean={statistics.mean(caps):.3f}")
    if basis_samples:
        log(f"basis binance-oracle USD over {len(basis_samples)} samples: "
            f"mean={statistics.mean(basis_samples):+.2f} "
            f"stdev={statistics.pstdev(basis_samples):.2f} "
            f"min={min(basis_samples):+.2f} max={max(basis_samples):+.2f}")
    log("RESULTS_JSON " + json.dumps(results))


if __name__ == "__main__":
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    asyncio.run(main(minutes))

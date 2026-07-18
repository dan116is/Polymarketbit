# POLYSIGNAL — Backtest Report (M2)
_generated 2026-07-16 19:00:56Z — dataset kachoio/polymarket-5-minute-crypto-up-down-markets_

## Setup
- Resolved BTC windows replayed: **14041** (1s top-of-book)
- Spot proxy: Binance 1s closes (same-source open => basis cancels in ln(S/S0)); outcomes scored against true Chainlink resolutions
- Taker fee assumed: **1000 bps** * min(p,1-p) per share (live Gamma value 2026-07; applied to the whole historical period — conservative)
- Hand latency simulated: **5s** (signal -> executed ask)
- Model iteration filters: persist=0s, stale_book=0s
- Train/test: time split at `1777731300` (2026-05-02 14:15:00+00:00) — config chosen on train, GATE 1 judged on test only
- Grid size: 240 configs

## Chosen config (by train EV)
- sigma half-life: **30s**, theta: **4c**, buffer: **3c**, band: **20-50s** before close

| set | signals | EV c/share | hit rate | Brier | mean ask | slip c |
|---|---|---|---|---|---|---|
| train | 4953 | -0.82 | 0.649 | 0.185 | 0.634 | +3.17 |
| test | 1973 | -2.14 | 0.598 | 0.207 | 0.597 | +3.77 |

Full-period signals at 5s latency: **6926**

## Latency sensitivity (test set)
| latency | signals | EV c/share | hit rate | Brier |
|---|---|---|---|---|
| 0s | 1989 | +1.57 | 0.601 | 0.205 |
| 2s | 1984 | -0.79 | 0.600 | 0.206 |
| 5s | 1973 | -2.14 | 0.598 | 0.207 |
| 10s | 1953 | -2.86 | 0.594 | 0.209 |

## By time-remaining at signal (test, 5s latency)
| tau bucket | signals | EV c/share | hit rate | Brier |
|---|---|---|---|---|
| 15-30s | 105 | -0.14 | 0.629 | 0.124 |
| 30-60s | 1868 | -2.26 | 0.596 | 0.212 |
| 60-90s | 0 | +0.00 | 0.000 | 0.000 |
| 90-120s | 0 | +0.00 | 0.000 | 0.000 |

## GATE 1 verdict
- EV/signal (test, 5s latency) >= +2c: see table
- **GATE 1: RED**

_Numbers trace to reports/backtest_results.json; raw signal frames reproducible via `python scripts/backtest.py`._
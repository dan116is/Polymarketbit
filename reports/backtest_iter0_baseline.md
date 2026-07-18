# POLYSIGNAL — Backtest Report (M2)
_generated 2026-07-16 18:46:15Z — dataset kachoio/polymarket-5-minute-crypto-up-down-markets_

## Setup
- Resolved BTC windows replayed: **14041** (1s top-of-book)
- Spot proxy: Binance 1s closes (same-source open => basis cancels in ln(S/S0)); outcomes scored against true Chainlink resolutions
- Taker fee assumed: **1000 bps** * min(p,1-p) per share (live Gamma value 2026-07; applied to the whole historical period — conservative)
- Hand latency simulated: **5s** (signal -> executed ask)
- Train/test: time split at `1777731300` (2026-05-02 14:15:00+00:00) — config chosen on train, GATE 1 judged on test only
- Grid size: 300 configs

## Chosen config (by train EV)
- sigma half-life: **30s**, theta: **4c**, buffer: **2c**, band: **15-90s** before close

| set | signals | EV c/share | hit rate | Brier | mean ask | slip c |
|---|---|---|---|---|---|---|
| train | 7192 | -0.98 | 0.665 | 0.177 | 0.651 | +3.15 |
| test | 2936 | -2.17 | 0.625 | 0.186 | 0.624 | +2.90 |

Full-period signals at 5s latency: **10128**

## Latency sensitivity (test set)
| latency | signals | EV c/share | hit rate | Brier |
|---|---|---|---|---|
| 0s | 2945 | +0.68 | 0.626 | 0.185 |
| 2s | 2942 | -1.10 | 0.626 | 0.185 |
| 5s | 2936 | -2.17 | 0.625 | 0.186 |
| 10s | 2919 | -2.47 | 0.623 | 0.187 |

## By time-remaining at signal (test, 5s latency)
| tau bucket | signals | EV c/share | hit rate | Brier |
|---|---|---|---|---|
| 15-30s | 35 | +5.38 | 0.686 | 0.079 |
| 30-60s | 194 | -2.19 | 0.562 | 0.174 |
| 60-90s | 1057 | -1.82 | 0.613 | 0.149 |
| 90-120s | 1650 | -2.56 | 0.639 | 0.213 |

## GATE 1 verdict
- EV/signal (test, 5s latency) >= +2c: see table
- **GATE 1: RED**

_Numbers trace to reports/backtest_results.json; raw signal frames reproducible via `python scripts/backtest.py`._
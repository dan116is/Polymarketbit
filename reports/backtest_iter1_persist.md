# POLYSIGNAL — Backtest Report (M2)
_generated 2026-07-16 18:55:08Z — dataset kachoio/polymarket-5-minute-crypto-up-down-markets_

## Setup
- Resolved BTC windows replayed: **14041** (1s top-of-book)
- Spot proxy: Binance 1s closes (same-source open => basis cancels in ln(S/S0)); outcomes scored against true Chainlink resolutions
- Taker fee assumed: **1000 bps** * min(p,1-p) per share (live Gamma value 2026-07; applied to the whole historical period — conservative)
- Hand latency simulated: **5s** (signal -> executed ask)
- Train/test: time split at `1777731300` (2026-05-02 14:15:00+00:00) — config chosen on train, GATE 1 judged on test only
- Grid size: 300 configs

## Chosen config (by train EV)
- sigma half-life: **30s**, theta: **4c**, buffer: **1c**, band: **15-90s** before close

| set | signals | EV c/share | hit rate | Brier | mean ask | slip c |
|---|---|---|---|---|---|---|
| train | 6779 | -0.82 | 0.682 | 0.176 | 0.667 | +1.61 |
| test | 2844 | -2.32 | 0.635 | 0.186 | 0.636 | +2.38 |

Full-period signals at 5s latency: **9623**

## Latency sensitivity (test set)
| latency | signals | EV c/share | hit rate | Brier |
|---|---|---|---|---|
| 0s | 2859 | +0.01 | 0.637 | 0.185 |
| 2s | 2853 | -1.61 | 0.636 | 0.185 |
| 5s | 2844 | -2.32 | 0.635 | 0.186 |
| 10s | 2829 | -2.68 | 0.633 | 0.187 |

## By time-remaining at signal (test, 5s latency)
| tau bucket | signals | EV c/share | hit rate | Brier |
|---|---|---|---|---|
| 15-30s | 40 | +0.01 | 0.550 | 0.112 |
| 30-60s | 254 | -3.50 | 0.591 | 0.157 |
| 60-90s | 2550 | -2.24 | 0.641 | 0.190 |
| 90-120s | 0 | +0.00 | 0.000 | 0.000 |

## GATE 1 verdict
- EV/signal (test, 5s latency) >= +2c: see table
- **GATE 1: RED**

_Numbers trace to reports/backtest_results.json; raw signal frames reproducible via `python scripts/backtest.py`._
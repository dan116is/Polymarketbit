# POLYSIGNAL

Personal signal system for Polymarket's 5-minute BTC Up/Down markets: a
deterministic fair-value engine + phone PWA + Telegram alerts. **No AI predicts
direction and the code never trades real money** — it computes the gap between
the mathematical probability of Up and the quoted order-book price, and says
`UP / DOWN / PASS` with a $1/$2/$5 stake recommendation, gated behind two
quantitative GO/NO-GO gates (backtest, then paper trading).

Built per `POLYSIGNAL-MASTER-PLAN.md` (the project INIT). Hard boundaries:
signals only, $5 max recommendation, −$10 daily tracked loss → 24h LIVE lock,
no martingale, LIVE display refuses to open unless GATE 1 + GATE 2 are green
in the database — there is no override in code.

## Architecture

```
WATCHER                     QUANT                   RISK              DELIVERY
Binance WS (spot proxy)     P_up = Φ(ln(S/S₀)/σ√τ)  EV threshold θ    Telegram alerts + /kill
RTDS Chainlink WS (oracle)  σ = EWMA 1s log-returns $1/$2/$5 ladder   PWA (WS live card)
CLOB REST (books)           EV vs ask − fee − buf   daily stop, locks
Gamma (slug → market meta)            ↓
                            SQLite: every tick, signal, outcome → ANALYST daily report
```

- **Market identification is deterministic**: `window_ts = now − (now % 300)` →
  slug `btc-updown-5m-{window_ts}` → Gamma returns `clobTokenIds`, fee, tick
  size. No searching, no indexing lag. (Verified live: detection ≤0.2s.)
- **Open price comes from the resolution source** (Chainlink BTC/USD), via
  Polymarket's RTDS websocket (`crypto_prices_chainlink` topic, symbol
  `btc/usd`, 1s resolution, ~70–120s backlog per subscribe, no incremental
  push → the client re-subscribes every 10s). Binance is only a low-latency
  proxy: the engine anchors on the last oracle point and extrapolates with
  Binance movement, so the ~$45 Binance↔oracle basis cancels by construction.
- **Fees are read from the API at runtime** (`takerBaseFee`, currently
  1000 bps = 10% × min(p, 1−p) per share) — never hardcoded.

## Layout

| path | what |
|---|---|
| `polysignal/quant.py` | fair value, EWMA vol, EV, stake ladder (pure functions) |
| `polysignal/feeds.py` | Binance WS, RTDS oracle WS, CLOB book poller (auto-reconnect) |
| `polysignal/engine.py` | per-window state machine (M1) |
| `polysignal/risk.py` | money rules: gates, daily stop, cooldown, kill-switch |
| `polysignal/store.py` | SQLite schema §4.4 + gate flags |
| `polysignal/delivery/` | Telegram notifier + command loop, PWA server |
| `polysignal/pwa/` | single-file phone app (served by the engine) |
| `web/` + `netlify.toml` | standalone browser-only monitor — deployable to Netlify, no server |
| `scripts/m0_discovery.py` | M0 spike: prove market id + book + oracle open, live |
| `scripts/backtest.py` | M2 harness: 15.7K windows replay, grid scan, GATE 1 |
| `scripts/gate2_check.py` | GATE 2 evaluation over the PAPER log |
| `scripts/analyst_report.py` | daily calibration report (SQL-traceable) |
| `reports/` | evidence: M0 log, backtest report, analyst reports |

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env            # fill TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

python scripts/m0_discovery.py 30          # discovery spike (M0 evidence)
python scripts/run_engine.py               # PAPER mode engine (M1/M4)
python scripts/run_engine.py --with-delivery   # + Telegram + PWA on :8787
python -m pytest tests/                    # incl. the 24h-lock risk test

# Backtest (M2): fetch datasets once, then run the grid
python scripts/fetch_binance_1s.py 2026-03-24 2026-05-18
python scripts/backtest.py                 # writes reports/backtest.md + GATE1 flag
python scripts/gate2_check.py              # after >=200 paper signals / 7 days
```

On the phone: open `http://<server>:8787`, Add to Home Screen. The card shows
UP/DOWN/PASS, countdown, P_fair vs market, edge and stake; it turns into an
explicit OFFLINE state if the server stops pushing (never a frozen screen).

Windows service: run `scripts/run_engine.py` via Task Scheduler / NSSM with
auto-restart; state survives restarts (SQLite, idempotent per window).

## Status vs plan (honest)

- **M0 ✓** — deterministic slug → market verified live; oracle open captured
  from RTDS Chainlink feed; fee/tick/minSize read from API; basis measured.
  Evidence: `reports/m0_discovery.log`.
- **M1 ✓** — engine runs, logs every window/tick to SQLite, WS auto-reconnect,
  idempotent upserts. (24h uptime soak pending on the permanent Windows box.)
- **M2 ✓ / GATE 1: RED → NO-GO for manual LIVE** — the model is calibrated
  (Brier 0.19) and has real edge at 0s latency (+3.8¢/share after fees), but
  it decays ~1¢/s of execution delay: all 300 grid configs and 3 model
  iterations are negative at 5s hand latency. Full verdict:
  `reports/GATE1_NO_GO.md`. The only realistic path to the edge is M6 (bot),
  a separate explicit decision.
- **M3 ✓ code** — Telegram alert + kill-switch commands, PWA. Phone-side
  verification (screenshot, ≤2s p95 alert) requires Daniel's device + token.
- **M4 ready** — PAPER mode with simulated 5s hand latency is the default
  engine mode; run 5–7 days then `gate2_check.py`.
- **M5 locked** — LIVE display auto-refuses while gates are red (tested).
- **M6 (auto-execution bot) is out of scope** — separate explicit decision.

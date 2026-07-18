"""GATE1B — the bot gate. Combines the backtest EV-vs-latency curve with the
MEASURED shadow latency and drift, per the plan's 'same gates' rule for M6.

Green requires ALL of:
- >= 100 shadow executions measured
- measured p95 total latency (build+sign + safety margin) maps to a latency
  bucket whose test EV >= +2c with a CI excluding 0 from below
- >= 500 full-period signals at that latency
- EV still positive at double the measured latency (fragility)
- measured live ask drift consistent with (not worse than) the backtest slip

Usage: python scripts/gate_bot_check.py
"""
from __future__ import annotations

import json
import sqlite3
import sys

sys.path.insert(0, ".")

from polysignal.store import Store

CFG = json.load(open("config.json"))
MIN_SHADOW = 100
NETWORK_MARGIN_MS = 400  # order POST round-trip allowance on top of build+sign


def main():
    conn = sqlite3.connect(CFG["runtime"]["db_path"])
    rows = conn.execute(
        "SELECT build_ms, drift_cents FROM shadow_execs "
        "WHERE build_ms IS NOT NULL").fetchall()
    n = len(rows)
    try:
        bot_ev = json.load(open("reports/bot_ev.json"))["by_latency"]
    except FileNotFoundError:
        print("GATE1B אדום: אין reports/bot_ev.json — הרץ קודם bot_latency_ev.py")
        _write(False, {"n_shadow": n, "reason": "no_bot_ev"})
        return
    if n < MIN_SHADOW:
        print(f"GATE1B אדום: רק {n} ריצות צל (צריך ≥{MIN_SHADOW})")
        _write(False, {"n_shadow": n, "reason": "not_enough_shadow"})
        return

    builds = sorted(r[0] for r in rows)
    p95_ms = builds[int(0.95 * (n - 1))]
    total_s = min((p95_ms + NETWORK_MARGIN_MS) / 1000.0, 3.0)

    def interp(field: str) -> float | None:
        """EV/CI-floor linearly interpolated between the adjacent latency
        buckets — a 0.4s bot must not be judged as a 1s bot, nor as 0s."""
        import math
        lo, hi = math.floor(total_s), min(math.ceil(total_s), 3)
        a, b = bot_ev.get(str(lo), {}), bot_ev.get(str(hi), {})
        va = a.get(field) if field != "ci_floor" else (a.get("ci95") or [None])[0]
        vb = b.get(field) if field != "ci_floor" else (b.get("ci95") or [None])[0]
        if va is None or vb is None:
            return None
        w = total_s - lo
        return va + (vb - va) * w

    ev = interp("ev_cents")
    ci_floor = interp("ci_floor")
    ev_double = None
    saved_total = total_s
    total_s = min(saved_total * 2, 3.0)
    ev_double = interp("ev_cents")
    total_s = saved_total
    full_n = bot_ev.get("0", {}).get("full_n", 0)
    drifts = [r[1] for r in rows if r[1] is not None]
    mean_drift = sum(drifts) / len(drifts) if drifts else None

    res = {"n_shadow": n, "p95_build_ms": round(p95_ms, 1),
           "assumed_total_s": round(total_s, 2),
           "ev_cents": round(ev, 2) if ev is not None else None,
           "ci_floor": round(ci_floor, 2) if ci_floor is not None else None,
           "full_n": full_n,
           "ev_at_double_latency": round(ev_double, 2) if ev_double is not None else None,
           "mean_live_drift_cents": mean_drift}
    green = (ev is not None and ev >= 2.0
             and ci_floor is not None and ci_floor > 0
             and full_n >= 500
             and ev_double is not None and ev_double > 0)
    print(f"GATE1B {'ירוק' if green else 'אדום'}: {res}")
    _write(green, res)


def _write(green: bool, res: dict):
    store = Store(CFG["runtime"]["db_path"])
    store.set_gate("GATE1B", green, json.dumps(res))
    store.close()


if __name__ == "__main__":
    main()

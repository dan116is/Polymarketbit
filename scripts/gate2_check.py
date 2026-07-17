"""GATE 2 evaluation over the PAPER log (plan §6).

Criteria (all must hold):
- >= 200 MEASURABLE signals in PAPER mode (valid exec price + outcome), with
  >= 90% coverage of all fired signals — systematic exec-sampling failure
  keeps the gate red instead of shrinking the denominator
- realized mean EV >= +1.5c/share after fees (exec price incl. 5s hand latency)
- calibration gap |mean P_fair - hit rate| <= 7 percentage points
- zero risk violations: RISK_VIOLATION events (defense-in-depth invariant in
  the engine) plus $5 recommendations while GATE 2 was red

Writes the GATE2 flag to the store. Red gate -> LIVE stays closed. No override.

Usage: python scripts/gate2_check.py
"""
from __future__ import annotations

import json
import sqlite3
import sys

sys.path.insert(0, ".")

from polysignal.quant import taker_fee_per_share
from polysignal.store import Store

CFG = json.load(open("config.json"))
MIN_SIGNALS = 200
MIN_EV_CENTS = 1.5
MAX_CALIB_GAP = 0.07
MIN_COVERAGE = 0.9
FEE_BPS_FALLBACK = 1000.0  # per-share fee recomputed at exec ask; live value from Gamma


def main():
    db = CFG["runtime"]["db_path"]
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT signal, stake_reco, p_fair_signal, exec_ask, outcome "
        "FROM windows WHERE mode='PAPER' AND signal IN ('UP','DOWN')").fetchall()
    n_fired = len(rows)
    violations = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='RISK_VIOLATION'").fetchone()[0]
    # $5 is allowed only after GATE 2 — any $5 recommendation in the log while
    # this script finds the gate red is itself a violation
    over_ladder = conn.execute(
        "SELECT COUNT(*) FROM windows WHERE signal IN ('UP','DOWN') "
        "AND stake_reco > 2").fetchone()[0]
    violations += over_ladder

    ev_share, hits, pfs = [], [], []
    for sig, stake, pf, exec_ask, outcome in rows:
        if outcome not in ("UP", "DOWN") or exec_ask is None \
                or not (0 < exec_ask < 1) or pf is None:
            continue
        won = sig == outcome
        fee = taker_fee_per_share(exec_ask, FEE_BPS_FALLBACK)
        ev_share.append((1 - exec_ask - fee) if won else (-exec_ask - fee))
        hits.append(1.0 if won else 0.0)
        pfs.append(pf if sig == "UP" else 1 - pf)

    n_valid = len(ev_share)
    coverage = (n_valid / n_fired) if n_fired else 0.0
    if n_valid == 0:
        res = {"n_fired": n_fired, "n_valid": 0, "coverage": round(coverage, 3),
               "risk_violations": violations}
        print(f"GATE 2 אדום: אין איתותים מדידים — {res}")
        _write(False, res)
        return

    ev_c = 100 * sum(ev_share) / n_valid
    hit = sum(hits) / n_valid
    calib_gap = abs(sum(pfs) / n_valid - hit)
    res = {"n_fired": n_fired, "n_valid": n_valid, "coverage": round(coverage, 3),
           "ev_cents": round(ev_c, 3), "hit_rate": round(hit, 4),
           "calib_gap": round(calib_gap, 4), "risk_violations": violations}
    green = (n_valid >= MIN_SIGNALS and coverage >= MIN_COVERAGE
             and ev_c >= MIN_EV_CENTS and calib_gap <= MAX_CALIB_GAP
             and violations == 0)
    print(f"GATE 2 {'ירוק' if green else 'אדום'}: {res}")
    _write(green, res)


def _write(green: bool, res: dict):
    store = Store(CFG["runtime"]["db_path"])
    store.set_gate("GATE2", green, json.dumps(res))
    store.close()


if __name__ == "__main__":
    main()

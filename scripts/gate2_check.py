"""GATE 2 evaluation over the PAPER log (plan §6).

Criteria (all must hold):
- >= 200 logged signals in PAPER mode
- realized mean EV >= +1.5c/share after fees (exec price incl. 5s hand latency)
- calibration gap |mean P_fair - hit rate| <= 7 percentage points
- zero RISK violations in the events log

Writes the GATE2 flag to the store. Red gate -> LIVE stays closed. No override.

Usage: python scripts/gate2_check.py
"""
from __future__ import annotations

import json
import sqlite3
import sys

sys.path.insert(0, ".")

from polysignal.store import Store

CFG = json.load(open("config.json"))
MIN_SIGNALS = 200
MIN_EV_CENTS = 1.5
MAX_CALIB_GAP = 0.07


def main():
    db = CFG["runtime"]["db_path"]
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT signal, stake_reco, p_fair_signal, exec_ask, fee, outcome, pnl "
        "FROM windows WHERE mode='PAPER' AND signal IN ('UP','DOWN') "
        "AND outcome IS NOT NULL").fetchall()
    n = len(rows)
    violations = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='RISK_VIOLATION'").fetchone()[0]

    if n == 0:
        print("GATE 2: עדיין אין איתותי PAPER בלוג — אדום")
        _write(False, {"n": 0})
        return

    ev_share = []
    hits = []
    pfs = []
    for sig, stake, pf, exec_ask, fee, outcome, pnl in rows:
        won = sig == outcome
        a = exec_ask if exec_ask is not None else None
        if a is None or not (0 < a < 1):
            continue
        f = fee if fee is not None else 0.0
        ev_share.append((1 - a - f) if won else (-a - f))
        hits.append(1.0 if won else 0.0)
        pfs.append(pf if sig == "UP" else 1 - pf)

    ev_c = 100 * sum(ev_share) / len(ev_share)
    hit = sum(hits) / len(hits)
    calib_gap = abs(sum(pfs) / len(pfs) - hit)
    res = {"n": n, "ev_cents": round(ev_c, 3), "hit_rate": round(hit, 4),
           "calib_gap": round(calib_gap, 4), "risk_violations": violations}
    green = (n >= MIN_SIGNALS and ev_c >= MIN_EV_CENTS
             and calib_gap <= MAX_CALIB_GAP and violations == 0)
    print(f"GATE 2 {'ירוק' if green else 'אדום'}: {res}")
    _write(green, res)


def _write(green: bool, res: dict):
    store = Store(CFG["runtime"]["db_path"])
    store.set_gate("GATE2", green, json.dumps(res))
    store.close()


if __name__ == "__main__":
    main()

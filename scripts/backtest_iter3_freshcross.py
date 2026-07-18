"""M2 model iteration 3 (final per budget): fresh-cross end-game rule.

Hypothesis from the baseline tau-bucket analysis: windows whose edge crosses
theta for the FIRST time in the last ~15-40s retain EV even at hand latency
(+5.4c at 15-30s in the baseline test set), because the book lags a nearly
decided outcome. Restricting the band directly (iteration 2) failed because
it re-includes windows that were already mispriced earlier (momentum).

Rule tested here: evaluate from 120s out, but only DISPLAY a signal if the
first crossing falls at tau <= tau_max. Reported honestly against the GATE 1
significance floor (>=500 signals) even where EV is positive.

Usage: python scripts/backtest_iter3_freshcross.py
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np
import pandas as pd
from scipy.special import ndtr

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from backtest import (GATE1, add_sigma, load_data, metrics,  # noqa: E402
                      simulate)

# best baseline config (reports/backtest_iter0_results.json)
HL, THETA, BUF, BAND = 30, 0.04, 0.02, (15, 120)
TAU_MAX_GRID = [30, 40, 45, 60]
HAND_LATENCY = 5


def main():
    t0 = time.time()
    df, bn = load_data()
    cid = df.condition_id.to_numpy()
    change = np.flatnonzero(np.r_[True, cid[1:] != cid[:-1]])
    counts = np.diff(np.r_[change, len(cid)])
    group_first = {"starts": change, "counts": counts}
    win_ts = np.sort(df.groupby("condition_id").window_ts.first().to_numpy())
    split_ts = int(win_ts[int(len(win_ts) * 0.7)])
    first_ts = df.groupby("condition_id").window_ts.first()

    sigma = add_sigma(df, bn, HL)
    spot, s_open, tau = df.spot.to_numpy(), df.s_open.to_numpy(), df.tau.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.log(spot / s_open) / (sigma * np.sqrt(tau))
    p_fair = ndtr(np.clip(z, -8, 8))

    out = {"config": {"halflife": HL, "theta": THETA, "buffer": BUF,
                      "band": list(BAND)}, "results": {}}
    for tau_max in TAU_MAX_GRID:
        row = {}
        for lat in (0, 2, 5, 10):
            sig = simulate(df, p_fair, theta=THETA, buffer=BUF, band=BAND,
                           latency_s=lat, group_first=group_first)
            sig = sig[sig.tau <= tau_max]  # fresh first-cross fell in end-game
            te = sig[sig.condition_id.map(first_ts >= split_ts)]
            tr = sig[sig.condition_id.map(first_ts < split_ts)]
            row[f"lat{lat}"] = {"train": metrics(tr), "test": metrics(te),
                                "full_n": int(len(sig))}
        out["results"][f"tau_max_{tau_max}"] = row
        m5 = row[f"lat{HAND_LATENCY}"]
        print(f"tau_max={tau_max}: 5s-latency train n={m5['train'].get('n',0)} "
              f"ev={m5['train'].get('ev_cents',0):+.2f}c | "
              f"test n={m5['test'].get('n',0)} "
              f"ev={m5['test'].get('ev_cents',0):+.2f}c "
              f"brier={m5['test'].get('brier',0):.3f}", flush=True)

    # GATE 1 assessment on the strongest tau_max by TRAIN ev (test-honest)
    best_key = max(out["results"],
                   key=lambda k: out["results"][k][f"lat{HAND_LATENCY}"]["train"].get("ev_cents", -99))
    best = out["results"][best_key]
    te5 = best[f"lat{HAND_LATENCY}"]["test"]
    te10 = best["lat10"]["test"]
    verdict = {
        "picked_on_train": best_key,
        "test_ev_5s": te5.get("ev_cents"),
        "test_n_5s": te5.get("n", 0),
        "full_period_n_5s": best[f"lat{HAND_LATENCY}"]["full_n"],
        "meets_ev": bool(te5.get("ev_cents", -99) >= GATE1["min_ev_cents"]),
        "meets_n_floor": bool(best[f"lat{HAND_LATENCY}"]["full_n"] >= GATE1["min_signals"]),
        "meets_brier": bool(te5.get("brier", 1) <= GATE1["max_brier"]),
        "survives_10s": bool(te10.get("ev_cents", -99) > 0),
    }
    verdict["gate1_green"] = all([verdict["meets_ev"], verdict["meets_n_floor"],
                                  verdict["meets_brier"], verdict["survives_10s"]])
    out["verdict"] = verdict
    with open("reports/backtest_iter3_results.json", "w") as f:
        json.dump(out, f, indent=1)
    print(f"verdict: {verdict}")
    print(f"done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()

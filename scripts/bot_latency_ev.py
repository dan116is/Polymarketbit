"""M6 decision table — the chosen backtest config evaluated at BOT latencies.

The hand died at 5s; the bot lives or dies between 0 and 3 seconds. This
produces the EV-vs-latency curve the GATE1B verdict reads, on the untouched
holdout, at 0/1/2/3s.

Usage: python scripts/bot_latency_ev.py [--theta 0.10]  ->  reports/bot_ev.*

--theta overrides the grid-selected EV threshold: the deep-improve audit found
theta=0.10 the best operating point (@1s +2.50c vs +0.99c at 0.06) by filtering
adversely-selected marginal signals. Passing it here regenerates the gate curve
at the operating point the engine actually fires at, keeping GATE1B honest.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

sys.path.insert(0, ".")

from scripts.backtest import GATE1, add_sigma, load_data, metrics, simulate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--theta", type=float, default=None,
                    help="override EV threshold (e.g. 0.10); default = grid best")
    args = ap.parse_args()
    best = json.load(open("reports/backtest_results.json"))["best"]
    if args.theta is not None:
        best = {**best, "theta": args.theta}
    hl, theta, buf = best["halflife"], best["theta"], best["buffer"]
    band = tuple(int(x) for x in best["band"].split("-"))
    print(f"config: hl={hl} theta={theta} buffer={buf} band={band}")

    df, bn = load_data()
    cid = df.condition_id.to_numpy()
    change = np.flatnonzero(np.r_[True, cid[1:] != cid[:-1]])
    counts = np.diff(np.r_[change, len(cid)])
    group_first = {"starts": change, "counts": counts}
    win_ts = np.sort(df.groupby("condition_id").window_ts.first().to_numpy())
    split_ts = int(win_ts[int(len(win_ts) * 0.7)])
    first_map = df.groupby("condition_id").window_ts.first()

    sigma = add_sigma(df, bn, hl)
    spot, s_open, tau = df.spot.to_numpy(), df.s_open.to_numpy(), df.tau.to_numpy()
    from scipy.special import ndtr
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.log(spot / s_open) / (sigma * np.sqrt(tau))
    p_fair = ndtr(np.clip(z, -8, 8))

    rows = {}
    for lat in (0, 1, 2, 3):
        sig = simulate(df, p_fair, theta=theta, buffer=buf, band=band,
                       latency_s=lat, group_first=group_first)
        te = sig[sig.condition_id.map(first_map >= split_ts)] if not sig.empty else sig
        m = metrics(te) if len(te) else {"n": 0}
        # iid bootstrap CI on the test EV
        if len(te):
            rng = np.random.default_rng(7)
            pnl = te.pnl_share.to_numpy()
            boots = np.array([rng.choice(pnl, len(pnl), replace=True).mean()
                              for _ in range(1000)]) * 100
            m["ci95"] = [round(float(np.percentile(boots, 2.5)), 2),
                         round(float(np.percentile(boots, 97.5)), 2)]
        m["full_n"] = int(metrics(sig)["n"]) if not sig.empty else 0
        rows[lat] = m
        print(f"lat={lat}s: n_test={m.get('n',0)} ev={m.get('ev_cents',0):+.2f}c "
              f"ci={m.get('ci95')} full_n={m['full_n']}")

    with open("reports/bot_ev.json", "w") as f:
        json.dump({"config": best, "by_latency": rows,
                   "gate1b_criteria": GATE1}, f, indent=1)

    L = ["# M6 — טבלת ההחלטה של הבוט: EV מול latency ביצוע",
         "",
         "הקונפיגורציה הנבחרת של הבקטסט, על סט הטסט הנקי, ב-latency של בוט.",
         f"(hl={hl}s, θ={theta*100:.0f}¢, buffer={buf*100:.0f}¢, band={band})",
         "",
         "| latency | איתותים (טסט) | EV ‏¢/מניה | CI‏ 95% | איתותים בכל התקופה |",
         "|---|---|---|---|---|"]
    for lat, m in rows.items():
        L.append(f"| {lat}s | {m.get('n',0)} | {m.get('ev_cents',0):+.2f} "
                 f"| {m.get('ci95','–')} | {m.get('full_n',0)} |")
    L += ["",
          "**קריאת הטבלה:** GATE1B דורש EV ≥ ‎+2¢ ב-latency שנמדד בפועל ע\"י בוט",
          "הצל (build+sign+רשת), עם ≥500 איתותים בתקופה ו-EV חיובי גם בכפול",
          "latency. המספר הנמדד מגיע מטבלת shadow_execs אחרי ימי ריצה.",
          ""]
    with open("reports/bot_ev.md", "w") as f:
        f.write("\n".join(L))
    print("wrote reports/bot_ev.md")


if __name__ == "__main__":
    main()

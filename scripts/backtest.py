"""M2 — Backtest harness over the historical 5-minute BTC Up/Down dataset.

Replays the fair-value model on ~15.7K resolved windows (1s top-of-book from
the kachoio HF dataset) with Binance 1s closes as the spot proxy, scores
against the TRUE Chainlink-resolved outcomes, grid-scans thresholds, and
evaluates GATE 1 on a time-based holdout (no peeking).

Inputs (already downloaded):
    data/hf/btc_markets.parquet   windows + outcomes
    data/hf/btc_ticks.parquet     1s top-of-book per window
    data/binance_1s.parquet       BTCUSDT 1s closes

Output:
    reports/backtest.md  + GATE1 flag written to the SQLite store.

Usage: python scripts/backtest.py [--fast]  (--fast: coarse grid, for smoke)
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time

import numpy as np
import pandas as pd
from scipy.special import ndtr

sys.path.insert(0, ".")

from polysignal.store import Store

FEE_BPS = 1000.0  # takerBaseFee read live from Gamma (2026-07); assumption for the historical period — see report
WINDOW_S = 300

GATE1 = {
    "min_ev_cents": 2.0,
    "min_signals": 500,
    "max_brier": 0.20,
    "fragility_latency_s": 10,
}


def fee_per_share(ask: np.ndarray) -> np.ndarray:
    return (FEE_BPS / 10_000.0) * np.minimum(ask, 1.0 - ask)


def load_data() -> pd.DataFrame:
    mk = pd.read_parquet("data/hf/btc_markets.parquet",
                         columns=["condition_id", "slug", "outcome", "n_ticks"])
    mk = mk[mk.outcome.isin(["Up", "Down"])].copy()
    mk["window_ts"] = mk.slug.str.rsplit("-", n=1).str[-1].astype("int64")
    mk["win_up"] = (mk.outcome == "Up").astype("int8")

    tk = pd.read_parquet("data/hf/btc_ticks.parquet",
                         columns=["condition_id", "t", "au", "ad", "sau", "sad"])
    bn = pd.read_parquet("data/binance_1s.parquet")

    df = tk.merge(mk[["condition_id", "window_ts", "win_up"]],
                  on="condition_id", how="inner")
    df = df.merge(bn, left_on="t", right_on="ts", how="left").drop(columns=["ts"])
    df = df.rename(columns={"close": "spot"})
    # S_open: spot at window start (same proxy source -> basis cancels in the ratio)
    open_map = bn.set_index("ts").close
    df["s_open"] = df.window_ts.map(open_map)
    df["tau"] = (df.window_ts + WINDOW_S - df.t).astype("float64")
    df = df[(df.tau > 0) & df.spot.notna() & df.s_open.notna()]
    df = df.sort_values(["condition_id", "t"], kind="stable").reset_index(drop=True)
    return df, bn


def add_sigma(df: pd.DataFrame, bn: pd.DataFrame, halflife_s: float) -> np.ndarray:
    """EWMA per-second vol of the Binance series, sampled at tick times."""
    r2 = np.log(bn.close / bn.close.shift()).pow(2)
    var = r2.ewm(halflife=halflife_s).mean()
    sigma = np.sqrt(var).to_numpy()
    sig_map = pd.Series(sigma, index=bn.ts.to_numpy())
    return df.t.map(sig_map).to_numpy()


def _run_length(cond: np.ndarray, is_start: np.ndarray) -> np.ndarray:
    """Consecutive True run length ending at each row (resets at group starts)."""
    idx = np.arange(len(cond))
    brk = np.where(~cond | is_start, idx, -1)
    last_break = np.maximum.accumulate(brk)
    rl = idx - last_break
    rl[is_start & cond] = 1  # a group's first row can start a run of 1
    return rl


def simulate(df: pd.DataFrame, p_fair: np.ndarray, *, theta: float, buffer: float,
             band: tuple[float, float], latency_s: int,
             group_first: dict, persist_s: int = 0,
             stale_book_s: int = 0) -> pd.DataFrame:
    """First qualifying signal per window, executed at ask(t_signal + latency).

    persist_s: require edge>theta for K consecutive seconds before signaling
    (anti momentum-chasing — model iteration 1).
    stale_book_s: additionally require the taken side's ask unchanged for K
    seconds (book stale vs fair value — model iteration 2).
    """
    au, ad = df.au.to_numpy(), df.ad.to_numpy()
    edge_up = p_fair - au - fee_per_share(au) - buffer
    edge_dn = (1.0 - p_fair) - ad - fee_per_share(ad) - buffer
    take_up = edge_up >= edge_dn
    edge = np.where(take_up, edge_up, edge_dn)
    tau = df.tau.to_numpy()
    qual = (edge > theta) & (tau >= band[0]) & (tau <= band[1]) \
        & np.isfinite(edge) & (au > 0) & (au < 1) & (ad > 0) & (ad < 1)

    if persist_s or stale_book_s:
        is_start = np.zeros(len(qual), dtype=bool)
        is_start[group_first["starts"]] = True
        if persist_s:
            qual = _run_length(qual, is_start) >= persist_s
        if stale_book_s:
            ask_taken = np.where(take_up, au, ad)
            unchanged = np.r_[True, ask_taken[1:] == ask_taken[:-1]]
            qual &= _run_length(unchanged, is_start) >= stale_book_s


    starts, counts = group_first["starts"], group_first["counts"]
    idx = _first_true_per_group(qual, starts, counts)
    sel = idx[idx >= 0]
    if len(sel) == 0:
        return pd.DataFrame()
    # dense 1s grid within each window -> exec row = signal row + latency
    exec_idx = np.minimum(sel + latency_s,
                          starts[np.searchsorted(starts, sel, side="right") - 1]
                          + counts[np.searchsorted(starts, sel, side="right") - 1] - 1)
    out = pd.DataFrame({
        "condition_id": df.condition_id.to_numpy()[sel],
        "t_signal": df.t.to_numpy()[sel],
        "tau": tau[sel],
        "side_up": take_up[sel],
        "p_fair": p_fair[sel],
        "edge": edge[sel],
        "win_up": df.win_up.to_numpy()[sel].astype(bool),
        "ask_sig": np.where(take_up[sel], au[sel], ad[sel]),
        "ask_exec": np.where(take_up[sel], au[exec_idx], ad[exec_idx]),
        "size_exec": np.where(take_up[sel],
                              df.sau.to_numpy()[exec_idx],
                              df.sad.to_numpy()[exec_idx]),
    })
    out = out[(out.ask_exec > 0) & (out.ask_exec < 1)]
    won = out.side_up == out.win_up
    fee = (FEE_BPS / 10_000.0) * np.minimum(out.ask_exec, 1 - out.ask_exec)
    out["pnl_share"] = np.where(won, 1.0 - out.ask_exec - fee, -out.ask_exec - fee)
    out["won"] = won
    p_side = np.where(out.side_up, out.p_fair, 1.0 - out.p_fair)
    out["brier"] = (p_side - won.astype(float)) ** 2
    return out


def _first_true_per_group(mask: np.ndarray, starts: np.ndarray,
                          counts: np.ndarray) -> np.ndarray:
    """Index of first True in each [start, start+count) block, -1 if none."""
    idx = np.full(len(starts), -1, dtype=np.int64)
    true_pos = np.flatnonzero(mask)
    if len(true_pos) == 0:
        return idx
    block = np.searchsorted(starts, true_pos, side="right") - 1
    valid = true_pos < starts[block] + counts[block]
    true_pos, block = true_pos[valid], block[valid]
    first = np.unique(block, return_index=True)
    idx[first[0]] = true_pos[first[1]]
    return idx


def metrics(sig: pd.DataFrame) -> dict:
    if sig.empty:
        return {"n": 0}
    return {
        "n": int(len(sig)),
        "ev_cents": float(sig.pnl_share.mean() * 100),
        "hit_rate": float(sig.won.mean()),
        "brier": float(sig.brier.mean()),
        "mean_ask": float(sig.ask_exec.mean()),
        "mean_tau": float(sig.tau.mean()),
        "slip_cents": float((sig.ask_exec - sig.ask_sig).mean() * 100),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--persist", type=int, default=0,
                    help="model iteration 1: edge must persist K seconds")
    ap.add_argument("--stale-book", type=int, default=0,
                    help="model iteration 2: ask unchanged K seconds")
    ap.add_argument("--bands", type=str, default="",
                    help='override band grid, e.g. "15-30,15-45,20-50"')
    args = ap.parse_args()

    t0 = time.time()
    print("loading data...", flush=True)
    df, bn = load_data()
    n_windows = df.condition_id.nunique()
    print(f"{n_windows} windows, {len(df)} ticks ({time.time()-t0:.0f}s)", flush=True)

    # group index (df sorted by condition_id, t; windows are dense 1s grids)
    cid = df.condition_id.to_numpy()
    change = np.flatnonzero(np.r_[True, cid[1:] != cid[:-1]])
    counts = np.diff(np.r_[change, len(cid)])
    group_first = {"starts": change, "counts": counts}

    # time-based split: first 70% of windows train, last 30% test
    win_ts = np.sort(df.groupby("condition_id").window_ts.first().to_numpy())
    split_ts = int(win_ts[int(len(win_ts) * 0.7)])
    is_test = df.window_ts.to_numpy() >= split_ts
    print(f"train/test split at window_ts={split_ts} "
          f"({pd.Timestamp(split_ts, unit='s', tz='UTC')})", flush=True)

    if args.fast:
        halflives, thetas, buffers, bands = [90], [0.06], [0.02], [(15, 120)]
    else:
        halflives = [30, 60, 90, 180]
        thetas = [0.04, 0.05, 0.06, 0.08, 0.10]
        buffers = [0.01, 0.02, 0.03]
        bands = [(15, 120), (15, 90), (30, 120), (20, 60), (15, 60)]
    if args.bands:
        bands = [tuple(int(x) for x in b.split("-"))
                 for b in args.bands.split(",")]

    HAND_LATENCY = 5
    rows = []
    spot, s_open, tau = df.spot.to_numpy(), df.s_open.to_numpy(), df.tau.to_numpy()
    for hl in halflives:
        sigma = add_sigma(df, bn, hl)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.log(spot / s_open) / (sigma * np.sqrt(tau))
        p_fair = ndtr(np.clip(z, -8, 8))
        for theta, buf, band in itertools.product(thetas, buffers, bands):
            sig = simulate(df, p_fair, theta=theta, buffer=buf, band=band,
                           latency_s=HAND_LATENCY, group_first=group_first,
                           persist_s=args.persist, stale_book_s=args.stale_book)
            if sig.empty:
                tr = te = {"n": 0}
            else:
                mask_te = sig.condition_id.map(
                    df.groupby("condition_id").window_ts.first() >= split_ts)
                tr, te = metrics(sig[~mask_te]), metrics(sig[mask_te])
            rows.append({"halflife": hl, "theta": theta, "buffer": buf,
                         "band": f"{band[0]}-{band[1]}",
                         "train": tr, "test": te})
            print(f"hl={hl} th={theta} buf={buf} band={band}: "
                  f"train n={tr.get('n',0)} ev={tr.get('ev_cents',0):+.2f}c | "
                  f"test n={te.get('n',0)} ev={te.get('ev_cents',0):+.2f}c", flush=True)

    # ---- pick config on TRAIN only: max EV with enough train signals ----------
    n_min_train = int(GATE1["min_signals"] * 0.7)
    eligible = [r for r in rows
                if r["train"].get("n", 0) >= n_min_train
                and r["train"].get("brier", 1) <= GATE1["max_brier"]]
    pick_pool = eligible if eligible else [r for r in rows if r["train"].get("n", 0) > 0]
    if not pick_pool:
        print("NO CONFIG PRODUCED ANY TRAIN SIGNAL — NO-GO by construction")
        best = None
    else:
        best = max(pick_pool, key=lambda r: r["train"]["ev_cents"])

    report = {"generated_utc": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
              "n_windows": int(n_windows), "split_ts": split_ts,
              "fee_bps_assumed": FEE_BPS, "hand_latency_s": HAND_LATENCY,
              "persist_s": args.persist, "stale_book_s": args.stale_book,
              "grid_size": len(rows), "best": best, "rows": rows}

    gate1_green = False
    lat_curve = {}
    tau_buckets = {}
    if best is not None:
        hl = best["halflife"]
        sigma = add_sigma(df, bn, hl)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.log(spot / s_open) / (sigma * np.sqrt(tau))
        p_fair = ndtr(np.clip(z, -8, 8))
        theta, buf = best["theta"], best["buffer"]
        band = tuple(int(x) for x in best["band"].split("-"))
        for lat in (0, 2, 5, 10):
            sig = simulate(df, p_fair, theta=theta, buffer=buf, band=band,
                           latency_s=lat, group_first=group_first,
                           persist_s=args.persist, stale_book_s=args.stale_book)
            m_test = metrics(sig[sig.condition_id.map(
                df.groupby("condition_id").window_ts.first() >= split_ts)]) \
                if not sig.empty else {"n": 0}
            lat_curve[lat] = m_test
        # tau buckets at hand latency (test set)
        sig = simulate(df, p_fair, theta=theta, buffer=buf, band=band,
                       latency_s=HAND_LATENCY, group_first=group_first,
                       persist_s=args.persist, stale_book_s=args.stale_book)
        sig_te = sig[sig.condition_id.map(
            df.groupby("condition_id").window_ts.first() >= split_ts)]
        for lo, hi in [(15, 30), (30, 60), (60, 90), (90, 120)]:
            b = sig_te[(sig_te.tau >= lo) & (sig_te.tau < hi)]
            tau_buckets[f"{lo}-{hi}s"] = metrics(b)
        te = lat_curve[HAND_LATENCY]
        te10 = lat_curve[GATE1["fragility_latency_s"]]
        gate1_green = (
            te.get("n", 0) >= GATE1["min_signals"] * 0.3  # test = 30% of period
            and te.get("ev_cents", -99) >= GATE1["min_ev_cents"]
            and te.get("brier", 1) <= GATE1["max_brier"]
            and te10.get("ev_cents", -99) > 0
            and (te.get("n", 0) + lat_curve[HAND_LATENCY].get("n", 0)) > 0
        )
        # full-period signal count also reported against the 500 threshold
        report["full_period_n"] = int(metrics(sig)["n"]) if not sig.empty else 0
        report["latency_curve_test"] = lat_curve
        report["tau_buckets_test"] = tau_buckets

    report["gate1_green"] = bool(gate1_green)
    with open("reports/backtest_results.json", "w") as f:
        json.dump(report, f, indent=1)
    write_markdown(report)
    store = Store(json.load(open("config.json"))["runtime"]["db_path"])
    store.set_gate("GATE1", gate1_green, "reports/backtest.md")
    store.close()
    print(f"GATE1 {'GREEN' if gate1_green else 'RED'} — report at reports/backtest.md "
          f"({time.time()-t0:.0f}s total)", flush=True)


def write_markdown(rep: dict):
    b = rep.get("best")
    lines = [
        "# POLYSIGNAL — דוח Backtest‏ (M2)",
        f"_הופק {rep['generated_utc']} — דאטהסט kachoio/polymarket-5-minute-crypto-up-down-markets_",
        "",
        "## מערך הבדיקה",
        f"- חלונות BTC פתורים ששוחזרו: **{rep['n_windows']}** (ספר ברזולוציית שנייה)",
        "- ספוט: נרות שנייה של Binance כ-proxy (פתיחה מאותו מקור ⇒ הבסיס מתבטל"
        " ב-ln(S/S₀)); התוצאות נשפטות מול ה-resolution האמיתי של Chainlink",
        f"- עמלת taker שהונחה: **{rep['fee_bps_assumed']:.0f}bps** × min(p,1−p) למניה"
        " (הערך החי מ-Gamma; הוחלה על כל התקופה — הנחה שמרנית)",
        f"- ‏latency יד מדומה: **{rep['hand_latency_s']} שניות** (מהאיתות עד ה-ask שבוצע)",
        f"- פילטרים של איטרציות מודל: התמדה={rep.get('persist_s', 0)}s, "
        f"ספר-קפוא={rep.get('stale_book_s', 0)}s",
        f"- ‏train/test: חלוקת זמן ב-`{rep['split_ts']}`"
        f" ({pd.Timestamp(rep['split_ts'], unit='s', tz='UTC')}) — הקונפיגורציה נבחרת"
        " על train, ‏GATE 1 נשפט על test בלבד",
        f"- גודל הסריקה: {rep['grid_size']} קונפיגורציות",
        "",
    ]
    if b is None:
        lines += ["## תוצאה: אפס איתותים — NO-GO", ""]
    else:
        tr, te = b["train"], b["test"]
        lines += [
            "## הקונפיגורציה שנבחרה (לפי EV על train)",
            f"- ‏half-life של σ: **{b['halflife']}s**, ‏θ: **{b['theta']*100:.0f}¢**, "
            f"‏buffer: **{b['buffer']*100:.0f}¢**, חלון איתות: **{b['band']}s** לפני הסגירה",
            "",
            "| סט | איתותים | EV ‏¢/מניה | פגיעה | Brier | ask ממוצע | סליפ ¢ |",
            "|---|---|---|---|---|---|---|",
            f"| train | {tr.get('n',0)} | {tr.get('ev_cents',0):+.2f} | {tr.get('hit_rate',0):.3f} "
            f"| {tr.get('brier',0):.3f} | {tr.get('mean_ask',0):.3f} | {tr.get('slip_cents',0):+.2f} |",
            f"| test | {te.get('n',0)} | {te.get('ev_cents',0):+.2f} | {te.get('hit_rate',0):.3f} "
            f"| {te.get('brier',0):.3f} | {te.get('mean_ask',0):.3f} | {te.get('slip_cents',0):+.2f} |",
            "",
            f"איתותים בכל התקופה ב-latency‏ 5s: **{rep.get('full_period_n', 0)}**",
            "",
            "## רגישות ל-latency (סט הטסט)",
            "| latency | איתותים | EV ‏¢/מניה | פגיעה | Brier |",
            "|---|---|---|---|---|",
        ]
        for lat, m in rep.get("latency_curve_test", {}).items():
            lines.append(f"| {lat}s | {m.get('n',0)} | {m.get('ev_cents',0):+.2f} "
                         f"| {m.get('hit_rate',0):.3f} | {m.get('brier',0):.3f} |")
        lines += ["", "## לפי זמן שנותר ברגע האיתות (טסט, ‏latency‏ 5s)",
                  "| טווח τ | איתותים | EV ‏¢/מניה | פגיעה | Brier |",
                  "|---|---|---|---|---|"]
        for k, m in rep.get("tau_buckets_test", {}).items():
            lines.append(f"| {k} | {m.get('n',0)} | {m.get('ev_cents',0):+.2f} "
                         f"| {m.get('hit_rate',0):.3f} | {m.get('brier',0):.3f} |")
    lines += [
        "",
        "## פסק דין GATE 1",
        f"- ‏EV לאיתות (טסט, ‏5s) ≥ ‎+2¢: {'עומד ✓' if rep.get('gate1_green') else 'לא עומד — ראה טבלה'}",
        f"- **GATE 1: {'ירוק' if rep.get('gate1_green') else 'אדום'}**",
        "",
        "_כל מספר ניתן למעקב ב-reports/backtest_results.json; שחזור מלא:"
        " `python scripts/backtest.py`._",
    ]
    with open("reports/backtest.md", "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()

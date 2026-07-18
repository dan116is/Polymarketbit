"""Fetch Binance 1s klines (public archive) for the backtest period and build
a single compact parquet: unix second -> close price.

Usage: python scripts/fetch_binance_1s.py 2026-03-24 2026-05-18
"""
from __future__ import annotations

import datetime as dt
import io
import sys
import zipfile

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s"
OUT = "data/binance_1s.parquet"


def main(start: str, end: str):
    d = dt.date.fromisoformat(start)
    d_end = dt.date.fromisoformat(end)
    frames = []
    while d <= d_end:
        url = f"{BASE}/BTCUSDT-1s-{d}.zip"
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            print(f"{d}: HTTP {r.status_code} — skipped", flush=True)
            d += dt.timedelta(days=1)
            continue
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = z.namelist()[0]
            df = pd.read_csv(z.open(name), header=None, usecols=[0, 4],
                             names=["open_time_ms", "close"])
        # Newer archives use microseconds; normalize to seconds.
        unit = 1_000_000 if df.open_time_ms.iloc[0] > 10**14 else 1_000
        df["ts"] = (df.open_time_ms // unit).astype("int64")
        frames.append(df[["ts", "close"]])
        print(f"{d}: {len(df)} rows", flush=True)
        d += dt.timedelta(days=1)
    out = pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    out.to_parquet(OUT, index=False)
    print(f"wrote {OUT}: {len(out)} seconds, "
          f"{out.ts.min()}..{out.ts.max()}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

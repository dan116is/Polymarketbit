"""Deterministic 5-minute window arithmetic. No market search, no indexing lag."""
from __future__ import annotations

import time

WINDOW_SECONDS = 300


def window_ts(now: float | None = None, window_seconds: int = WINDOW_SECONDS) -> int:
    """Start (unix seconds) of the window containing `now`."""
    t = int(now if now is not None else time.time())
    return t - (t % window_seconds)


def next_window_ts(now: float | None = None, window_seconds: int = WINDOW_SECONDS) -> int:
    return window_ts(now, window_seconds) + window_seconds


def t_remaining(now: float | None = None, window_seconds: int = WINDOW_SECONDS) -> float:
    """Seconds until the current window closes."""
    t = now if now is not None else time.time()
    return window_ts(t, window_seconds) + window_seconds - t


def slug_for(ts: int, prefix: str = "btc-updown-5m") -> str:
    return f"{prefix}-{ts}"

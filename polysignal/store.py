"""SQLite persistence — every window, every signal, every outcome (plan §4.4).

Idempotent per (window_ts, mode): re-running never duplicates rows.
GATE flags live here too and are only written by the report scripts.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    window_ts       INTEGER NOT NULL,
    mode            TEXT    NOT NULL CHECK (mode IN ('BACKTEST','PAPER','LIVE')),
    s_open          REAL,
    source_open     TEXT,
    s_close         REAL,
    outcome         TEXT CHECK (outcome IS NULL OR outcome IN ('UP','DOWN')),
    p_fair_signal   REAL,
    ask_up          REAL,
    ask_down        REAL,
    bid_up          REAL,
    bid_down        REAL,
    fee             REAL,
    edge            REAL,
    signal          TEXT CHECK (signal IS NULL OR signal IN ('UP','DOWN','PASS')),
    stake_reco      REAL,
    t_remaining_sig REAL,
    latency_ms      REAL,
    exec_ask        REAL,
    acted           INTEGER DEFAULT 0,
    pnl             REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (window_ts, mode)
);

CREATE TABLE IF NOT EXISTS ticks (
    ts          REAL NOT NULL,
    window_ts   INTEGER NOT NULL,
    s_binance   REAL,
    s_oracle    REAL,
    basis       REAL,
    ask_up      REAL,
    ask_down    REAL,
    p_fair      REAL,
    sigma_1s    REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_window ON ticks (window_ts);

CREATE TABLE IF NOT EXISTS risk_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
    gate       TEXT PRIMARY KEY CHECK (gate IN ('GATE1','GATE2','GATE1B','GATE2B')),
    green      INTEGER NOT NULL,
    report     TEXT,
    updated_at REAL NOT NULL
);

-- M6 shadow executions: the full order pipeline WITHOUT sending. Measures the
-- real signal->order-ready latency and the ask drift over it — the numbers the
-- real-bot decision hinges on.
CREATE TABLE IF NOT EXISTS shadow_execs (
    window_ts     INTEGER NOT NULL,
    mode          TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    stake_usd     REAL    NOT NULL,
    t_signal      REAL    NOT NULL,
    build_ms      REAL,
    ask_at_signal REAL,
    ask_at_ready  REAL,
    drift_cents   REAL,
    PRIMARY KEY (window_ts, mode)
);

CREATE TABLE IF NOT EXISTS events (
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind, ts);
CREATE INDEX IF NOT EXISTS idx_windows_mode ON windows (mode, window_ts);

-- edge-decay evidence: the taken side's top-of-book at fixed offsets after
-- each signal. This is the number a future M6 decision hinges on.
CREATE TABLE IF NOT EXISTS exec_samples (
    window_ts INTEGER NOT NULL,
    mode      TEXT    NOT NULL,
    dt_s      REAL    NOT NULL,
    bid       REAL,
    ask       REAL,
    bid_depth REAL,
    ask_depth REAL,
    PRIMARY KEY (window_ts, mode, dt_s)
);
"""

TICKS_RETENTION_S = 14 * 86400


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # analyst/gate scripts read the same DB from other processes while the
        # engine commits at 1Hz — don't fail on a momentary write lock
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.execute("DELETE FROM ticks WHERE ts < ?",
                          (time.time() - TICKS_RETENTION_S,))
        self.conn.commit()

    # ---- windows ----------------------------------------------------------
    def upsert_window(self, window_ts: int, mode: str, **fields) -> None:
        now = time.time()
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{c}=excluded.{c}" for c in fields)
        self.conn.execute(
            f"INSERT INTO windows (window_ts, mode, {cols}, created_at, updated_at) "
            f"VALUES (?, ?, {placeholders}, ?, ?) "
            f"ON CONFLICT (window_ts, mode) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
            (window_ts, mode, *fields.values(), now, now),
        )
        self.conn.commit()

    def get_window(self, window_ts: int, mode: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM windows WHERE window_ts=? AND mode=?",
            (window_ts, mode)).fetchone()
        return dict(row) if row else None

    def log_exec_sample(self, window_ts: int, mode: str, dt_s: float,
                        bid: float | None, ask: float | None,
                        bid_depth: float | None, ask_depth: float | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO exec_samples VALUES (?, ?, ?, ?, ?, ?, ?)",
            (window_ts, mode, dt_s, bid, ask, bid_depth, ask_depth))
        self.conn.commit()

    def window_count(self, mode: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM windows"
        args: tuple = ()
        if mode:
            q += " WHERE mode=?"
            args = (mode,)
        return self.conn.execute(q, args).fetchone()[0]

    # ---- ticks -------------------------------------------------------------
    def log_tick(self, **fields) -> None:
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        self.conn.execute(
            f"INSERT INTO ticks ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        self.conn.commit()

    # ---- risk state --------------------------------------------------------
    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )
        self.conn.commit()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # ---- gates --------------------------------------------------------------
    def set_gate(self, gate: str, green: bool, report: str = "") -> None:
        self.conn.execute(
            "INSERT INTO gates (gate, green, report, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (gate) DO UPDATE SET green=excluded.green, report=excluded.report, "
            "updated_at=excluded.updated_at",
            (gate, int(green), report, time.time()),
        )
        self.conn.commit()

    def gate_green(self, gate: str) -> bool:
        row = self.conn.execute(
            "SELECT green FROM gates WHERE gate=?", (gate,)).fetchone()
        return bool(row and row[0])

    # ---- events --------------------------------------------------------------
    def log_event(self, kind: str, detail: str = "") -> None:
        self.conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
                          (time.time(), kind, detail))
        self.conn.commit()

    def close(self):
        self.conn.close()

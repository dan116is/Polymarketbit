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
    outcome         TEXT CHECK (outcome IN ('UP','DOWN',NULL)),
    p_fair_signal   REAL,
    ask_up          REAL,
    ask_down        REAL,
    bid_up          REAL,
    bid_down        REAL,
    fee             REAL,
    edge            REAL,
    signal          TEXT CHECK (signal IN ('UP','DOWN','PASS',NULL)),
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
    gate       TEXT PRIMARY KEY CHECK (gate IN ('GATE1','GATE2')),
    green      INTEGER NOT NULL,
    report     TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind, ts);
CREATE INDEX IF NOT EXISTS idx_windows_mode ON windows (mode, window_ts);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
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

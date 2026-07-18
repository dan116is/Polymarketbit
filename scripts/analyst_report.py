"""ANALYST — daily calibration report from the SQLite log (mechanical part).

Produces reports/analyst_YYYY-MM-DD.md with every number traceable to a SQL
query printed alongside it. The LLM part of ANALYST (reading this, proposing
threshold experiments) happens offline in Claude and every proposal goes to
Daniel for manual approval — nothing here changes config.

Usage: python scripts/analyst_report.py [YYYY-MM-DD]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time

CFG = json.load(open("config.json"))


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d", time.gmtime())
    t0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
    t1 = t0 + 86400
    conn = sqlite3.connect(CFG["runtime"]["db_path"])
    L = [f"# דוח ANALYST יומי — {day}", ""]

    sql = ("SELECT mode, COUNT(*), SUM(signal IN ('UP','DOWN')), "
           "AVG(CASE WHEN signal=outcome THEN 1.0 WHEN signal IN ('UP','DOWN') THEN 0.0 END), "
           "SUM(pnl) FROM windows WHERE window_ts>=? AND window_ts<? GROUP BY mode")
    L += ["## חלונות / איתותים / פגיעה / רווח-הפסד", "```sql", sql, "```", "",
          "| מצב | חלונות | איתותים | פגיעה | PnL |", "|---|---|---|---|---|"]
    for mode, n, s, hit, pnl in q(conn, sql, (t0, t1)):
        L.append(f"| {mode} | {n} | {s or 0} | "
                 f"{hit if hit is not None else '—'} | {pnl if pnl is not None else '—'} |")

    sql = ("SELECT CAST(t_remaining_sig/30 AS INT)*30, COUNT(*), AVG(pnl), "
           "AVG(p_fair_signal), AVG(CASE WHEN signal=outcome THEN 1.0 ELSE 0.0 END) "
           "FROM windows WHERE signal IN ('UP','DOWN') AND outcome IS NOT NULL "
           "AND window_ts>=? AND window_ts<? GROUP BY 1 ORDER BY 1")
    L += ["", "## לפי זמן שנותר ברגע האיתות", "```sql", sql, "```", "",
          "| טווח τ (שניות) | n | ‏PnL ממוצע | ‏P_fair ממוצע | פגיעה |", "|---|---|---|---|---|"]
    for b, n, pnl, pf, hit in q(conn, sql, (t0, t1)):
        L.append(f"| {b}-{b+30} | {n} | {pnl} | {pf} | {hit} |")

    sql = ("SELECT AVG((p_fair_signal - (outcome='UP'))*(p_fair_signal - (outcome='UP'))) "
           "FROM windows WHERE p_fair_signal IS NOT NULL AND outcome IS NOT NULL "
           "AND window_ts>=? AND window_ts<?")
    brier = q(conn, sql, (t0, t1))[0][0]
    L += ["", "## ‏Brier (כל ה-P_fair שנרשמו ברגעי איתות)", "```sql", sql, "```",
          f"", f"Brier = **{brier}**", ""]

    sql = ("SELECT kind, COUNT(*) FROM events WHERE ts>=? AND ts<? GROUP BY kind")
    L += ["## אירועים", "```sql", sql, "```", ""]
    for kind, n in q(conn, sql, (t0, t1)):
        L.append(f"- {kind}: {n}")

    sql = ("SELECT AVG(basis), MIN(basis), MAX(basis) FROM ticks "
           "WHERE ts>=? AND ts<? AND basis IS NOT NULL")
    row = q(conn, sql, (t0, t1))[0]
    L += ["", "## בסיס Binance↔אורקל (דולר)", "```sql", sql, "```",
          f"", f"mean={row[0]} min={row[1]} max={row[2]}", ""]

    # uptime from heartbeat coverage (1/minute when the engine loop is alive)
    sql = "SELECT COUNT(*) FROM events WHERE kind='HEARTBEAT' AND ts>=? AND ts<?"
    hb = q(conn, sql, (t0, t1))[0][0]
    L += ["## ‏Uptime (מתוך HEARTBEAT לדקה)", "```sql", sql, "```", "",
          f"heartbeats={hb} ⇒ ‏uptime ≈ **{100.0*hb/1440:.1f}%** מהיממה", ""]
    sql = ("SELECT kind, COUNT(*) FROM events WHERE kind IN ('FEED_DOWN','FEED_UP') "
           "AND ts>=? AND ts<? GROUP BY kind")
    for kind, n in q(conn, sql, (t0, t1)):
        L.append(f"- {kind}: {n}")

    # measured edge decay after signals — the number M6 hinges on
    sql = ("SELECT dt_s, COUNT(*), AVG(ask) FROM exec_samples "
           "WHERE window_ts>=? AND window_ts<? GROUP BY dt_s ORDER BY dt_s")
    rows = q(conn, sql, (t0, t1))
    if rows:
        L += ["", "## דעיכת ה-ask אחרי איתות (נמדד חי)", "```sql", sql, "```", "",
              "| שניות אחרי איתות | n | ‏ask ממוצע |", "|---|---|---|"]
        for dt, n, ask in rows:
            L.append(f"| +{dt:.0f}s | {n} | {ask:.4f} |")

    out = f"reports/analyst_{day}.md"
    with open(out, "w") as f:
        f.write("\n".join(L))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

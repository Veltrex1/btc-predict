"""Keep data/ticks.sqlite from filling the disk.

    uv run python -m recorder.compact            # run once (the learning loop runs it every cycle)

- Raw `spot` ticks older than 2 days are rolled into `spot_1s` (one average mid per
  venue per second, which is all the studies use) and deleted.
- High-volume raw feeds are trimmed: BRTI 5 Hz and Kalshi order-book deltas after
  3 days, Deribit ticks after 7 days, Kalshi public trades after 14 days.
- Kept permanently: Kalshi 1 s books, BRTI 1 Hz (incl. the forming settlement
  average), Polymarket US books, market results, spot_1s.

Deletes run in small batches with a commit each, so the recorder keeps writing
in between. SQLite reuses freed pages, so the file stops growing rather than shrinking.
"""
from __future__ import annotations

import sqlite3
import time

from pipeline.config import DATA_DIR

DB = DATA_DIR / "ticks.sqlite"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

KEEP = {  # table: (age limit ms, time column, extra filter)
    "brti": (3 * DAY_MS, "source_ts", "feed = '5hz'"),
    "kalshi_ob": (3 * DAY_MS, "ts", None),
    "deribit": (7 * DAY_MS, "ts", None),
    "kalshi_trades": (14 * DAY_MS, "ts", None),
}


def connect(db=DB) -> sqlite3.Connection:
    c = sqlite3.connect(db, timeout=120)
    c.execute("PRAGMA busy_timeout = 120000")
    c.execute("CREATE TABLE IF NOT EXISTS spot_1s (sec INTEGER, venue TEXT, mid REAL, PRIMARY KEY (sec, venue))")
    return c


def has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def roll_spot(c: sqlite3.Connection, now_ms: int, keep_ms: int = 2 * DAY_MS, chunk_ms: int = HOUR_MS,
              deadline: float | None = None) -> int:
    """Aggregate raw spot older than keep_ms into spot_1s an hour at a time, deleting the raw rows."""
    cutoff = now_ms - keep_ms
    lo = c.execute("SELECT min(ts) FROM spot").fetchone()[0]
    moved = 0
    while lo is not None and lo < cutoff:
        if deadline and time.time() > deadline:
            break
        hi = min(lo - lo % chunk_ms + chunk_ms, cutoff)
        c.execute("INSERT OR REPLACE INTO spot_1s (sec, venue, mid) "
                  "SELECT ts / 1000, venue, AVG((bid + ask) / 2.0) FROM spot "
                  "WHERE ts >= ? AND ts < ? AND bid IS NOT NULL AND ask IS NOT NULL GROUP BY ts / 1000, venue",
                  (lo, hi))
        moved += c.execute("DELETE FROM spot WHERE ts >= ? AND ts < ?", (lo, hi)).rowcount
        c.commit()
        lo = hi
    return moved


def trim(c: sqlite3.Connection, table: str, now_ms: int, batch: int = 200_000,
         deadline: float | None = None) -> int:
    """Delete rows older than the table's limit, oldest first, in rowid batches (insertion order ~ time)."""
    if not has_table(c, table):
        return 0
    age, col, extra = KEEP[table]
    cond = f"{col} < ?" + (f" AND {extra}" if extra else "")
    gone = 0
    while not (deadline and time.time() > deadline):
        n = c.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {cond} LIMIT ?)",
                      (now_ms - age, batch)).rowcount
        c.commit()
        gone += n
        if n < batch:
            break
    return gone


def run(db=DB, max_minutes: float = 150, now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    deadline = time.time() + max_minutes * 60
    c = connect(db)
    try:
        out = {"spot_rows_rolled": roll_spot(c, now_ms, deadline=deadline)}
        for t in KEEP:
            out[f"{t}_deleted"] = trim(c, t, now_ms, deadline=deadline)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return out
    finally:
        c.close()


if __name__ == "__main__":
    print(run())

"""Bounded readers for the recorder's database (data/ticks.sqlite).

The raw tables hold hundreds of millions of rows after a few weeks, far more
than the 2 GB droplet can hold in memory. Everything here reads one time range
at a time through the `ts` indexes and summarizes to one row per second in SQL,
so a study's memory use depends on the length of a day, not of the recording.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pandas as pd

from .config import DATA_DIR

DB = DATA_DIR / "ticks.sqlite"
DAY_MS = 86_400_000
WINDOW_MS = 15 * 60_000


def connect(db=DB) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)


def has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def markets(c: sqlite3.Connection, close_from: int | None = None, close_to: int | None = None) -> pd.DataFrame:
    q, p = "SELECT * FROM kalshi_markets WHERE 1=1", []
    if close_from is not None:
        q += " AND close_ts >= ?"
        p.append(close_from)
    if close_to is not None:
        q += " AND close_ts < ?"
        p.append(close_to)
    return pd.read_sql(q, c, params=p)


def days(c: sqlite3.Connection, last_n: int | None = None) -> list[tuple[int, int]]:
    """UTC day ranges [start_ms, end_ms) covering the recorded settled markets."""
    lo, hi = c.execute("SELECT min(close_ts), max(close_ts) FROM kalshi_markets WHERE result IS NOT NULL").fetchone()
    if lo is None:
        return []
    start = (lo // DAY_MS) * DAY_MS
    out = [(t, t + DAY_MS) for t in range(start, hi + 1, DAY_MS)]
    return out[-last_n:] if last_n else out


def spot_mid_1s(c: sqlite3.Connection, t0: int, t1: int) -> pd.Series:
    """Composite spot mid per second (median across venues of each venue's average mid in that second)."""
    parts = [pd.read_sql(
        "SELECT ts / 1000 AS sec, venue, AVG((bid + ask) / 2.0) AS mid FROM spot "
        "WHERE ts >= ? AND ts < ? AND bid IS NOT NULL AND ask IS NOT NULL GROUP BY sec, venue",
        c, params=(t0, t1))]
    if has_table(c, "spot_1s"):  # older seconds already compacted by recorder.compact
        parts.append(pd.read_sql("SELECT sec, venue, mid FROM spot_1s WHERE sec >= ? AND sec < ?",
                                 c, params=(t0 // 1000, t1 // 1000)))
    s = pd.concat([p for p in parts if len(p)]) if any(len(p) for p in parts) else parts[0]
    if s.empty:
        return pd.Series(dtype=float)
    wide = s.groupby(["sec", "venue"])["mid"].mean().unstack().sort_index().ffill(limit=5)
    return wide.median(axis=1)


def kalshi_book(c: sqlite3.Connection, t0: int, t1: int) -> pd.DataFrame:
    return pd.read_sql("SELECT ts, ticker, yes_bid, yes_ask, yes_bid_sz, yes_ask_sz FROM kalshi_book "
                       "WHERE ts >= ? AND ts < ? AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL",
                       c, params=(t0, t1))


def brti_1hz(c: sqlite3.Connection, t0: int, t1: int) -> pd.DataFrame:
    if not has_table(c, "brti"):
        return pd.DataFrame(columns=["source_ts", "value", "settle_avg", "settle_n"])
    return pd.read_sql("SELECT source_ts, value, settle_avg, settle_n FROM brti "
                       "WHERE feed = '1hz' AND value IS NOT NULL AND source_ts >= ? AND source_ts < ?",
                       c, params=(t0, t1))


def pmus_book(c: sqlite3.Connection, t0: int, t1: int) -> pd.DataFrame:
    if not has_table(c, "pmus_book"):
        return pd.DataFrame(columns=["ts", "slug", "best_bid", "bid_sz", "best_ask", "ask_sz"])
    return pd.read_sql("SELECT ts, slug, best_bid, bid_sz, best_ask, ask_sz FROM pmus_book WHERE ts >= ? AND ts < ?",
                       c, params=(t0, t1))


def pmus_live(c: sqlite3.Connection, t0: int, t1: int) -> pd.DataFrame:
    """Real-time Polymarket US top of book (one row per change) from the WebSocket recorder.
    Unlike `pmus_book` (the 30 s cached REST copy) these are the prices an order would actually meet."""
    cols = ["ts", "slug", "state", "best_bid", "bid_sz", "best_ask", "ask_sz"]
    if not has_table(c, "pmus_ws"):
        return pd.DataFrame(columns=cols)
    return pd.read_sql(f"SELECT {', '.join(cols)} FROM pmus_ws WHERE ts >= ? AND ts < ?", c, params=(t0, t1))


def pmus_results(c: sqlite3.Connection, t0: int, t1: int) -> pd.DataFrame:
    """Polymarket US settlements (settle_px = 1.0 when Up won) for windows closing in [t0, t1)."""
    if not has_table(c, "pmus_results"):
        return pd.DataFrame(columns=["slug", "settle_px"])
    return pd.read_sql("SELECT slug, settle_px FROM pmus_results WHERE close_ts >= ? AND close_ts < ? "
                       "AND settle_px IS NOT NULL", c, params=(t0, t1))


def by_day(c: sqlite3.Connection, last_n: int | None = None, lookback_ms: int = WINDOW_MS + 60_000) \
        -> Iterator[tuple[int, int, int, pd.DataFrame]]:
    """For each day: (data_from, data_to, day_start, markets closing that day). Data starts `lookback_ms`
    early so windows that opened before midnight are complete."""
    for d0, d1 in days(c, last_n):
        mk = markets(c, d0, d1)
        if len(mk):
            yield d0 - lookback_ms, d1, d0, mk

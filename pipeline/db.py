"""DuckDB storage. All timestamps are naive UTC.

btc_1m.ts is the bar START; a bar covers [ts, ts + 1 minute).
kalshi_candles.ts is the candle END (Kalshi's end_period_ts).
Kalshi prices are stored in dollars (0..1).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS btc_1m (
    source VARCHAR NOT NULL,
    ts TIMESTAMP NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume DOUBLE,
    taker_buy DOUBLE,
    PRIMARY KEY (source, ts)
);
CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker VARCHAR PRIMARY KEY,
    open_time TIMESTAMP,
    close_time TIMESTAMP,
    target DOUBLE,
    result VARCHAR,
    expiration_value DOUBLE,
    status VARCHAR
);
CREATE TABLE IF NOT EXISTS kalshi_candles (
    ticker VARCHAR NOT NULL,
    ts TIMESTAMP NOT NULL,
    yes_bid DOUBLE,
    yes_ask DOUBLE,
    last_price DOUBLE,
    volume DOUBLE,
    PRIMARY KEY (ticker, ts)
);
"""

COLUMNS = {
    "btc_1m": ["source", "ts", "open", "high", "low", "close", "volume", "taker_buy"],
    "kalshi_markets": ["ticker", "open_time", "close_time", "target", "result", "expiration_value", "status"],
    "kalshi_candles": ["ticker", "ts", "yes_bid", "yes_ask", "last_price", "volume"],
}


def connect(path: str | Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(path or DB_PATH)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def upsert(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    cols = COLUMNS[table]
    df = df.reindex(columns=cols)
    con.register("_upsert_df", df)
    try:
        con.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _upsert_df")
    finally:
        con.unregister("_upsert_df")
    return len(df)


def load_bars(con, source: str, start=None, end=None) -> pd.DataFrame:
    """1-minute bars indexed by bar start (naive UTC)."""
    q = "SELECT ts, open, high, low, close, volume, taker_buy FROM btc_1m WHERE source = ?"
    params: list = [source]
    if start is not None:
        q += " AND ts >= ?"
        params.append(pd.Timestamp(start).to_pydatetime())
    if end is not None:
        q += " AND ts < ?"
        params.append(pd.Timestamp(end).to_pydatetime())
    df = con.execute(q + " ORDER BY ts", params).df()
    return df.set_index("ts")


def load_kalshi_markets(con) -> pd.DataFrame:
    return con.execute("SELECT * FROM kalshi_markets ORDER BY open_time").df()


def load_kalshi_candles(con) -> pd.DataFrame:
    return con.execute("SELECT * FROM kalshi_candles ORDER BY ticker, ts").df()

"""Backfill 1-minute BTC-USD candles from Coinbase Exchange.

Coinbase is one of the venues in the CF Benchmarks BRTI index Kalshi settles on,
so it is the closest freely available proxy for the settlement price. The public
API returns at most 300 candles per request, so 2 years is ~3,500 requests
(about 10 minutes). Chunks already in the database are skipped.

    uv run python -m pipeline.backfill_coinbase --days 730
"""
from __future__ import annotations

import argparse
import time

import pandas as pd

from . import db
from .config import COINBASE_REST
from .http import get, session

SOURCE = "coinbase"
CHUNK_MIN = 300


def parse_candles(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=db.COLUMNS["btc_1m"])
    df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    return pd.DataFrame({
        "source": SOURCE,
        "ts": pd.to_datetime(df["time"], unit="s"),
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "volume": df["volume"].astype(float),
        "taker_buy": float("nan"),
    }).sort_values("ts").drop_duplicates("ts")


def fetch_range(s, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    r = get(s, f"{COINBASE_REST}/products/BTC-USD/candles",
            params={"granularity": 60, "start": start.isoformat() + "Z", "end": end.isoformat() + "Z"})
    return parse_candles(r.json())


def run(days: int = 730, force: bool = False, db_path=None, pause: float = 0.12) -> None:
    con = db.connect(db_path)
    s = session()
    end = pd.Timestamp.now("UTC").tz_localize(None).floor("min")
    start = (end - pd.Timedelta(days=days)).floor("D")
    existing = set()
    if not force:
        existing = set(pd.to_datetime(con.execute(
            "SELECT ts FROM btc_1m WHERE source = ? AND ts >= ?", [SOURCE, start.to_pydatetime()]
        ).df()["ts"]))
    t = start
    fetched = 0
    while t < end:
        t2 = min(t + pd.Timedelta(minutes=CHUNK_MIN), end)
        expected = int((t2 - t).total_seconds() // 60)
        have = sum(1 for k in range(expected) if t + pd.Timedelta(minutes=k) in existing) if existing else 0
        # Coinbase omits minutes with no trades, so treat >=90% as complete.
        if have < expected * 0.9:
            df = fetch_range(s, t, t2 - pd.Timedelta(minutes=1))
            fetched += db.upsert(con, "btc_1m", df)
            time.sleep(pause)
        if t.minute == 0 and t.hour == 0:
            print(f"{t.date()}  (+{fetched} bars so far)", flush=True)
        t = t2
    total = con.execute("SELECT count(*), min(ts), max(ts) FROM btc_1m WHERE source = ?", [SOURCE]).fetchone()
    print(f"coinbase total: {total[0]} bars, {total[1]} → {total[2]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    run(args.days, args.force)


if __name__ == "__main__":
    main()

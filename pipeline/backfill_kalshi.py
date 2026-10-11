"""Backfill every settled Kalshi 15-minute BTC market (series KXBTC15M) and,
optionally, its 1-minute price history.

Markets give the official target and result: the ground truth the class grades
against. Candles give the YES bid/ask at each minute, which is what lets us
measure the model's edge against the market.

    uv run python -m pipeline.backfill_kalshi --probe        # print raw payloads
    uv run python -m pipeline.backfill_kalshi                # markets only (fast)
    uv run python -m pipeline.backfill_kalshi --candles      # + minute candles (slow, resumable)
"""
from __future__ import annotations

import argparse
import json
import time

import pandas as pd

from . import db
from .config import KALSHI_BASE, SERIES
from .http import get, session
from .kalshi import parse_candles, parse_market


def iter_markets(s, base: str = KALSHI_BASE, series: str = SERIES, min_close_ts: int | None = None,
                 status: str | None = "settled", historical: bool = False):
    path = "/historical/markets" if historical else "/markets"
    cursor = None
    while True:
        params = {"series_ticker": series, "limit": 1000}
        if status:
            params["status"] = status
        if min_close_ts:
            params["min_close_ts"] = min_close_ts
        if cursor:
            params["cursor"] = cursor
        r = get(s, base + path, params=params, ok_404=True)
        if r is None:  # endpoint not available
            return
        body = r.json()
        for m in body.get("markets", []):
            yield m
        cursor = body.get("cursor")
        if not cursor:
            return
        time.sleep(0.06)


def fetch_candles(s, ticker: str, start: pd.Timestamp, end: pd.Timestamp, historical: bool = False,
                  base: str = KALSHI_BASE, series: str = SERIES) -> pd.DataFrame:
    params = {"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()), "period_interval": 1}
    paths = ([f"/historical/markets/{ticker}/candlesticks"] if historical else []) + \
        [f"/series/{series}/markets/{ticker}/candlesticks"]
    for p in paths:
        r = get(s, base + p, params=params, ok_404=True)
        if r is not None:
            return parse_candles(ticker, r.json().get("candlesticks", []))
    return parse_candles(ticker, [])


def probe() -> None:
    s = session()
    for hist in (False, True):
        for m in iter_markets(s, historical=hist):
            print(("historical " if hist else "") + "market sample:")
            print(json.dumps(m, indent=2)[:3000])
            print("parsed:", parse_market(m))
            op, cl = pd.Timestamp(m["open_time"]), pd.Timestamp(m["close_time"])
            r = get(s, f"{KALSHI_BASE}/series/{SERIES}/markets/{m['ticker']}/candlesticks",
                    params={"start_ts": int(op.timestamp()), "end_ts": int(cl.timestamp()), "period_interval": 1},
                    ok_404=True)
            print("candles sample:", json.dumps(r.json() if r else None, indent=2)[:2000])
            break
    r = get(s, f"{KALSHI_BASE}/markets", params={"series_ticker": SERIES, "status": "open"})
    print("open markets:", json.dumps(r.json(), indent=2)[:2000])


def run(candles: bool = False, since: str | None = None, db_path=None) -> None:
    con = db.connect(db_path)
    s = session()
    min_close = None
    if since:
        min_close = int(pd.Timestamp(since).timestamp())
    else:
        last = con.execute("SELECT max(close_time) FROM kalshi_markets WHERE result IS NOT NULL").fetchone()[0]
        if last is not None:
            min_close = int(pd.Timestamp(last).timestamp()) - 3600

    seen = 0
    for hist in (True, False):
        batch = []
        for m in iter_markets(s, min_close_ts=min_close, historical=hist):
            batch.append(parse_market(m))
            if len(batch) >= 2000:
                seen += db.upsert(con, "kalshi_markets", pd.DataFrame(batch))
                print(f"markets: {seen}", flush=True)
                batch = []
        seen += db.upsert(con, "kalshi_markets", pd.DataFrame(batch))
    n, lo, hi = con.execute("SELECT count(*), min(open_time), max(close_time) FROM kalshi_markets").fetchone()
    print(f"kalshi markets in db: {n} ({lo} → {hi}); fetched {seen} this run")

    if not candles:
        return
    todo = con.execute("""
        SELECT m.ticker, m.open_time, m.close_time FROM kalshi_markets m
        WHERE m.result IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM kalshi_candles c WHERE c.ticker = m.ticker)
        ORDER BY m.close_time DESC
    """).df()
    print(f"candles to fetch: {len(todo)}")
    for i, row in enumerate(todo.itertuples(index=False)):
        df = fetch_candles(s, row.ticker, pd.Timestamp(row.open_time), pd.Timestamp(row.close_time),
                           historical=True)
        if df.empty:  # remember that we tried, so a resume skips it
            df = pd.DataFrame([{"ticker": row.ticker, "ts": pd.Timestamp(row.open_time)}])
        db.upsert(con, "kalshi_candles", df)
        if i % 200 == 0:
            print(f"candles {i}/{len(todo)}", flush=True)
        time.sleep(0.06)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="print sample raw payloads and exit")
    ap.add_argument("--candles", action="store_true", help="also fetch 1-minute candles per market")
    ap.add_argument("--since", help="only markets closing after this date (default: resume)")
    args = ap.parse_args()
    if args.probe:
        probe()
    else:
        run(args.candles, args.since)


if __name__ == "__main__":
    main()

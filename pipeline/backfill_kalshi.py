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
    paths = [f"/series/{series}/markets/{ticker}/candlesticks"] + \
        ([f"/historical/markets/{ticker}/candlesticks"] if historical else [])
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


def run(candles: bool = False, since: str | None = None, db_path=None, batch_size: int = 25, pause: float = 0.5, hist_every: int = 1) -> None:
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
    cutoff = historical_cutoff(s)
    todo = todo.sort_values("close_time").reset_index(drop=True)
    live = todo[pd.to_datetime(todo["close_time"]) >= cutoff]
    hist = todo[pd.to_datetime(todo["close_time"]) < cutoff]
    # archived markets have no batch endpoint: one request each, so optionally sample every Nth window
    hist = hist.iloc[::hist_every]
    print(f"historical cutoff {cutoff}: {len(live)} live markets (batched), "
          f"{len(hist)} archived markets (1 request each, every {hist_every})", flush=True)
    done = 0
    for i in range(0, len(live), batch_size):
        chunk = live.iloc[i:i + batch_size]
        start, end = pd.Timestamp(chunk["open_time"].min()), pd.Timestamp(chunk["close_time"].max())
        frames = []
        if len(chunk) * ((end - start).total_seconds() / 60 + 1) <= 10_000:  # API cap per request
            try:
                frames = fetch_candles_batch(s, list(chunk["ticker"]), start, end)
            except Exception as e:  # noqa: BLE001
                print(f"batch failed ({e}); falling back to single requests", flush=True)
        got = {f["ticker"].iloc[0] for f in frames if not f.empty}
        for r in chunk.itertuples(index=False):
            if r.ticker not in got:
                frames.append(_single(s, r, historical=False))
        db.upsert(con, "kalshi_candles", pd.concat(frames))
        done += len(chunk)
        if (i // batch_size) % 20 == 0:
            print(f"live candles {done}/{len(live)}  (through {end})", flush=True)
        time.sleep(pause)
    buf = []
    for j, r in enumerate(hist.itertuples(index=False)):
        buf.append(_single(s, r, historical=True))
        time.sleep(pause)
        if len(buf) >= 200:
            db.upsert(con, "kalshi_candles", pd.concat(buf))
            buf = []
            print(f"archived candles {j + 1}/{len(hist)}  (through {r.close_time})", flush=True)
    if buf:
        db.upsert(con, "kalshi_candles", pd.concat(buf))
    done += len(hist)
    print(f"candles done: {done}")


def historical_cutoff(s, base: str = KALSHI_BASE) -> pd.Timestamp:
    r = get(s, base + "/historical/cutoff", ok_404=True)
    if r is None:
        return pd.Timestamp.min
    t = pd.Timestamp(r.json()["market_settled_ts"])
    return t.tz_convert(None) if t.tzinfo else t


def _single(s, r, historical: bool) -> pd.DataFrame:
    start, end = pd.Timestamp(r.open_time), pd.Timestamp(r.close_time)
    params = {"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()), "period_interval": 1}
    path = f"/historical/markets/{r.ticker}/candlesticks" if historical else \
        f"/series/{SERIES}/markets/{r.ticker}/candlesticks"
    try:
        resp = get(s, KALSHI_BASE + path, params=params, ok_404=True)
        df = parse_candles(r.ticker, resp.json().get("candlesticks", []) if resp is not None else [])
    except Exception as e:  # noqa: BLE001 - leave it for the next resume
        print(f"skip {r.ticker}: {e}", flush=True)
        return pd.DataFrame(columns=["ticker", "ts"])
    if df.empty:  # remember that we tried, so a resume skips it
        df = pd.DataFrame([{"ticker": r.ticker, "ts": start}])
    return df


def fetch_candles_batch(s, tickers: list[str], start: pd.Timestamp, end: pd.Timestamp,
                        base: str = KALSHI_BASE) -> list[pd.DataFrame]:
    """Up to ~25 consecutive markets per request (the API caps a request at 10,000 candles)."""
    r = get(s, base + "/markets/candlesticks", params={
        "market_tickers": ",".join(tickers), "start_ts": int(start.timestamp()),
        "end_ts": int(end.timestamp()), "period_interval": 1})
    return [parse_candles(m["market_ticker"], m.get("candlesticks", [])) for m in r.json().get("markets", [])]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="print sample raw payloads and exit")
    ap.add_argument("--candles", action="store_true", help="also fetch 1-minute candles per market")
    ap.add_argument("--since", help="only markets closing after this date (default: resume)")
    ap.add_argument("--hist-every", type=int, default=1,
                    help="for archived markets (one request each), fetch every Nth window (default 1 = all)")
    args = ap.parse_args()
    if args.probe:
        probe()
    else:
        run(args.candles, args.since, hist_every=args.hist_every)


if __name__ == "__main__":
    main()

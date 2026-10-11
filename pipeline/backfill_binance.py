"""Backfill 1-minute BTCUSDT klines from Binance's public archive (data.binance.vision).

Monthly zips go back to 2017-08; the current month is filled from daily zips.
Re-running skips months that are already complete.

    uv run python -m pipeline.backfill_binance --start 2017-08
"""
from __future__ import annotations

import argparse
import io
import zipfile
from datetime import date, timedelta

import pandas as pd

from . import db
from .config import BINANCE_ARCHIVE
from .http import get, session

SOURCE = "binance"
SYMBOL = "BTCUSDT"
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
              "trades", "taker_buy", "taker_buy_quote", "ignore"]


def parse_kline_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        with zf.open(zf.namelist()[0]) as f:
            raw = pd.read_csv(f, header=None, names=KLINE_COLS, dtype=str)
    raw = raw[pd.to_numeric(raw["open_time"], errors="coerce").notna()]  # drop header row if present
    t = raw["open_time"].astype("int64")
    # Spot archives switched from milliseconds to microseconds in 2025.
    unit = "us" if t.iloc[0] > 10**14 else "ms"
    out = pd.DataFrame({
        "source": SOURCE,
        "ts": pd.to_datetime(t, unit=unit),
        "open": raw["open"].astype(float),
        "high": raw["high"].astype(float),
        "low": raw["low"].astype(float),
        "close": raw["close"].astype(float),
        "volume": raw["volume"].astype(float),
        "taker_buy": raw["taker_buy"].astype(float),
    })
    return out.drop_duplicates("ts")


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) < (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _have_rows(con, start: pd.Timestamp, end: pd.Timestamp) -> int:
    return con.execute("SELECT count(*) FROM btc_1m WHERE source = ? AND ts >= ? AND ts < ?",
                       [SOURCE, start.to_pydatetime(), end.to_pydatetime()]).fetchone()[0]


def run(start: str = "2017-08", force: bool = False, db_path=None) -> None:
    con = db.connect(db_path)
    s = session()
    today = date.today()
    first = date.fromisoformat(start + "-01") if len(start) == 7 else date.fromisoformat(start)
    this_month = today.replace(day=1)

    for y, m in _months(first, this_month):
        m_start = pd.Timestamp(year=y, month=m, day=1)
        m_end = m_start + pd.offsets.MonthBegin(1)
        expected = int((m_end - m_start).total_seconds() // 60)
        if not force and _have_rows(con, m_start, m_end) >= expected * 0.98:
            continue
        url = f"{BINANCE_ARCHIVE}/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{y}-{m:02d}.zip"
        r = get(s, url, ok_404=True, timeout=120)
        if r is None:
            print(f"{y}-{m:02d}: not in archive")
            continue
        n = db.upsert(con, "btc_1m", parse_kline_zip(r.content))
        print(f"{y}-{m:02d}: {n} bars")

    d = max(this_month, first)
    while d < today:
        d_start = pd.Timestamp(d)
        if force or _have_rows(con, d_start, d_start + pd.Timedelta(days=1)) < 1400:
            url = f"{BINANCE_ARCHIVE}/daily/klines/{SYMBOL}/1m/{SYMBOL}-1m-{d.isoformat()}.zip"
            r = get(s, url, ok_404=True, timeout=60)
            if r is not None:
                n = db.upsert(con, "btc_1m", parse_kline_zip(r.content))
                print(f"{d}: {n} bars")
        d += timedelta(days=1)
    total = con.execute("SELECT count(*), min(ts), max(ts) FROM btc_1m WHERE source = ?", [SOURCE]).fetchone()
    print(f"binance total: {total[0]} bars, {total[1]} → {total[2]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2017-08", help="YYYY-MM (default 2017-08, the start of the archive)")
    ap.add_argument("--force", action="store_true", help="re-download months already present")
    args = ap.parse_args()
    run(args.start, args.force)


if __name__ == "__main__":
    main()

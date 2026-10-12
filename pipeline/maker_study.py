"""Did resting (maker) orders on KXBTC15M make money, after adverse selection?

    uv run python -m pipeline.maker_study --sample 600      # download trades for a random sample of windows
    uv run python -m pipeline.maker_study --analyze         # P&L of maker fills by price, time left, period

Every Kalshi trade has a taker and a maker. The public trade feed gives the
price, size and the taker's side, so the maker's side and price follow, and
settlement tells us who won. That is the realized P&L of real resting orders,
including the adverse selection a simple "filled at the bid" backtest ignores.
Makers pay no fee on this series (fee_type "quadratic").
"""
from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd

from . import db
from .config import DATA_DIR, KALSHI_BASE
from .http import get, session

OUT = DATA_DIR / "maker_trades.parquet"


def fetch_trades(s, ticker: str) -> list[dict]:
    rows, cursor = [], None
    while True:
        params = {"ticker": ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = get(s, f"{KALSHI_BASE}/markets/trades", params=params).json()
        rows += r.get("trades", [])
        cursor = r.get("cursor")
        time.sleep(0.5)
        if not cursor or not r.get("trades"):
            return rows


def download(sample: int, since: str, seed: int = 0) -> None:
    con = db.connect(read_only=True)
    m = con.execute("SELECT ticker, close_time, target, result FROM kalshi_markets "
                    "WHERE result IN ('yes','no') AND close_time >= ?", [since]).df()
    con.close()
    done = set(pd.read_parquet(OUT)["ticker"].unique()) if OUT.exists() else set()
    todo = m[~m["ticker"].isin(done)].sample(n=min(sample, len(m)), random_state=seed)
    s = session()
    frames = [pd.read_parquet(OUT)] if OUT.exists() else []
    for i, row in enumerate(todo.itertuples(index=False)):
        tr = pd.DataFrame(fetch_trades(s, row.ticker))
        if tr.empty:
            continue
        tr["close_time"], tr["result"] = row.close_time, row.result
        frames.append(tr[["ticker", "trade_id", "created_time", "yes_price_dollars", "count_fp",
                          "taker_outcome_side", "close_time", "result"]])
        if (i + 1) % 25 == 0:
            pd.concat(frames).to_parquet(OUT)
            print(f"{i + 1}/{len(todo)} windows", flush=True)
    pd.concat(frames).to_parquet(OUT)


def maker_fills(tr: pd.DataFrame) -> pd.DataFrame:
    t = tr.copy()
    t["yes_price"] = t["yes_price_dollars"].astype(float)
    t["count"] = t["count_fp"].astype(float)
    t["ts"] = pd.to_datetime(t["created_time"], utc=True, format="ISO8601").dt.tz_convert(None)
    t["mins_left"] = (pd.to_datetime(t["close_time"]) - t["ts"]).dt.total_seconds() / 60
    # the maker holds the side opposite the taker, at that side's price
    t["maker_side"] = np.where(t["taker_outcome_side"] == "yes", "no", "yes")
    t["price"] = np.where(t["maker_side"] == "yes", t["yes_price"], 1 - t["yes_price"])
    t["win"] = (t["maker_side"] == t["result"]).astype(int)
    t["pnl"] = t["win"] - t["price"]           # per contract, no maker fee
    return t


def analyze() -> None:
    t = maker_fills(pd.read_parquet(OUT))
    t = t[(t["mins_left"] > 0) & (t["price"] > 0.005) & (t["price"] < 0.995)]
    print(f"{len(t):,} maker fills across {t['ticker'].nunique()} windows, "
          f"{t['count'].sum():,.0f} contracts")
    w = lambda g: pd.Series({"fills": len(g), "contracts": g["count"].sum(),  # noqa: E731
                             "avg_price": np.average(g["price"], weights=g["count"]),
                             "win_rate": np.average(g["win"], weights=g["count"]),
                             "pnl_per_contract": np.average(g["pnl"], weights=g["count"]),
                             "windows": g["ticker"].nunique()})
    t["price_b"] = pd.cut(t["price"], [0, .1, .3, .5, .7, .8, .9, .95, 1])
    t["time_b"] = pd.cut(t["mins_left"], [0, 1, 2, 5, 10, 15], labels=["<1m", "1-2m", "2-5m", "5-10m", "10-15m"])
    pd.set_option("display.width", 200)
    print("\nAll maker fills by price (contract-weighted):")
    print(t.groupby("price_b", observed=True).apply(w, include_groups=False).round(4).to_string())
    print("\nBy time left x price:")
    print(t.groupby(["time_b", "price_b"], observed=True).apply(w, include_groups=False).round(4).to_string())

    # the specific rule from the candle study: maker buys of the favourite (70-90c) with 6-14 min left
    rule = t[(t["price"] >= 0.70) & (t["price"] <= 0.90) & (t["mins_left"] >= 6) & (t["mins_left"] <= 14)]
    per_window = rule.groupby("ticker").apply(
        lambda g: pd.Series({"pnl": np.average(g["pnl"], weights=g["count"]), "close": g["close_time"].iloc[0]}),
        include_groups=False)
    per_window["half"] = np.where(per_window["close"] < per_window["close"].median(), "first half", "second half")
    print("\nRULE: maker buys of the favourite at 70-90c with 6-14 min left (one average per window):")
    print(per_window.groupby("half")["pnl"].agg(["count", "mean", lambda x: x.std() / np.sqrt(len(x))])
          .rename(columns={"<lambda_0>": "se"}).round(4).to_string())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--since", default="2026-08-12")
    ap.add_argument("--analyze", action="store_true")
    args = ap.parse_args()
    if args.sample:
        download(args.sample, args.since)
    if args.analyze:
        analyze()


if __name__ == "__main__":
    main()

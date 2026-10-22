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

OUT = DATA_DIR / "maker_trades.parquet"      # legacy raw trades (migrated into AGG, then removed)
AGG = DATA_DIR / "maker_agg.parquet"         # per window x price bin x time bin: contracts and summed P&L
PBINS = [0.0, 0.02, 0.10, 0.30, 0.40, 0.60, 0.70, 0.90, 0.98, 1.0001]
TBINS = [0.0, 0.25, 2.0, 5.0, 6.0, 14.0, 15.01]


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


def aggregate(fills: pd.DataFrame) -> pd.DataFrame:
    """Collapse maker fills (from maker_fills) to window x price bin x minutes-left bin."""
    f = fills[(fills["mins_left"] > 0) & (fills["price"] > 0) & (fills["price"] < 1)]
    if f.empty:
        return pd.DataFrame(columns=["ticker", "close_time", "plo", "tlo", "contracts", "pnl_x", "fills"])
    f = f.assign(plo=pd.cut(f["price"], PBINS, right=False, labels=PBINS[:-1]).astype(float),
                 tlo=pd.cut(f["mins_left"], TBINS, right=False, labels=TBINS[:-1]).astype(float),
                 pnl_x=f["pnl"] * f["count"])
    return f.groupby(["ticker", "close_time", "plo", "tlo"], observed=True).agg(
        contracts=("count", "sum"), pnl_x=("pnl_x", "sum"), fills=("count", "size")).reset_index()


def migrate_raw() -> None:
    """Fold a legacy raw-trades parquet into AGG in chunks (bounded memory), then delete it."""
    if not OUT.exists():
        return
    import pyarrow.parquet as pq
    parts = []
    for batch in pq.ParquetFile(OUT).iter_batches(batch_size=500_000):
        parts.append(aggregate(maker_fills(batch.to_pandas())))
    agg = pd.concat(parts).groupby(["ticker", "close_time", "plo", "tlo"], as_index=False)[
        ["contracts", "pnl_x", "fills"]].sum() if parts else pd.DataFrame()
    if AGG.exists():
        agg = pd.concat([pd.read_parquet(AGG), agg]).drop_duplicates(["ticker", "plo", "tlo"], keep="last")
    agg.to_parquet(AGG)
    OUT.unlink()
    print(f"migrated raw trades into {AGG} ({agg['ticker'].nunique()} windows); removed {OUT}")


def download(sample: int, since: str, seed: int = 0) -> None:
    """Fetch trade history for `sample` more settled windows and store only the aggregated maker P&L."""
    migrate_raw()
    con = db.connect(read_only=True)
    m = con.execute("SELECT ticker, close_time, target, result FROM kalshi_markets "
                    "WHERE result IN ('yes','no') AND close_time >= ?", [since]).df()
    con.close()
    old = pd.read_parquet(AGG) if AGG.exists() else pd.DataFrame()
    done = set(old["ticker"].unique()) if len(old) else set()
    todo = m[~m["ticker"].isin(done)]
    todo = todo.sample(n=min(sample, len(todo)), random_state=seed) if len(todo) else todo
    s = session()
    new = []
    for i, row in enumerate(todo.itertuples(index=False)):
        tr = pd.DataFrame(fetch_trades(s, row.ticker))
        if tr.empty:
            continue
        tr["close_time"], tr["result"] = row.close_time, row.result
        new.append(aggregate(maker_fills(tr)))
        if (i + 1) % 25 == 0 or i + 1 == len(todo):
            pd.concat([old] + new).to_parquet(AGG)
            print(f"{i + 1}/{len(todo)} windows", flush=True)


def range_pnl(agg: pd.DataFrame, lo: float, hi: float, tl: float, th: float) -> pd.DataFrame:
    """Contract-weighted maker P&L per window for fills priced [lo, hi) with [tl, th) minutes left."""
    g = agg[(agg.plo >= lo) & (agg.plo < hi) & (agg.tlo >= tl) & (agg.tlo < th)]
    if g.empty:
        return pd.DataFrame(columns=["pnl", "t"])
    w = g.groupby("ticker").agg(pnl_x=("pnl_x", "sum"), contracts=("contracts", "sum"), t=("close_time", "first"))
    w = w[w.contracts > 0]
    return pd.DataFrame({"pnl": w.pnl_x / w.contracts, "t": w.t})


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
    migrate_raw()
    if not AGG.exists():
        print("no maker data yet")
        return
    agg = pd.read_parquet(AGG)
    print(f"{agg['ticker'].nunique()} windows, {agg['contracts'].sum():,.0f} maker contracts")
    t = agg.groupby(["tlo", "plo"]).agg(contracts=("contracts", "sum"), pnl_x=("pnl_x", "sum"),
                                         windows=("ticker", "nunique")).reset_index()
    t["pnl_per_contract"] = t.pnl_x / t.contracts
    pd.set_option("display.width", 200)
    print(t[["tlo", "plo", "windows", "contracts", "pnl_per_contract"]].round(4).to_string(index=False))
    for lo, hi, tl, th in [(0.70, 0.90, 6, 14), (0.40, 0.60, 6, 14), (0.10, 0.30, 2, 5), (0.90, 0.98, 0.25, 2)]:
        r = range_pnl(agg, lo, hi, tl, th)
        print(f"{lo:.2f}-{hi:.2f} at {tl}-{th} min: {len(r)} windows, mean {r.pnl.mean() * 100:+.2f}c")


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

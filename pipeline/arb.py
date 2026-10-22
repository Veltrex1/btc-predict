"""Kalshi vs Polymarket US cross-venue arbitrage on the same 15-minute BTC window.

    uv run python -m pipeline.arb

Both venues settle on CF Benchmarks BRTI averaged over the final minute, so
"Up/YES on one venue + Down/NO on the other" pays exactly $1 if both settle the
same way. Two combinations, priced each second from the recorder's books:

  A: buy Kalshi YES at its ask       + buy PM-US Down (sell Up) at the Up bid
  B: buy PM-US Up at its ask         + buy Kalshi NO at (1 - Kalshi YES bid)

Edge = 1 - cost - Kalshi taker fee - PM-US taker fee (0.0695 * p * (1-p)).
Caveat: Polymarket US averages [T-60s, T) while Kalshi's window includes the
close tick, so a near-tie can settle differently on the two venues; that risk
is reported separately from the edge.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from .config import DATA_DIR
from .strategy import fee as kalshi_fee

DB = DATA_DIR / "ticks.sqlite"
PMUS_THETA = 0.0695


def pmus_fee(count: float, price: float) -> float:
    return PMUS_THETA * count * price * (1 - price)


def load(db=DB, t0: int | None = None, t1: int | None = None,
         mk: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Polymarket US books, Kalshi books and markets for [t0, t1) ms (bounded; the whole range if omitted)."""
    from . import ticks
    c = ticks.connect(db)
    try:
        t0 = 0 if t0 is None else t0
        t1 = 2**62 if t1 is None else t1
        mk = ticks.markets(c) if mk is None else mk
        return ticks.pmus_book(c, t0, t1), ticks.kalshi_book(c, t0, t1), \
            mk[["ticker", "close_ts", "target", "result", "expiration_value"]]
    finally:
        c.close()


KEEP = ["ticker", "sec", "close_ts", "secs_left", "target", "expiration_value", "cost_a", "cost_b",
        "edge_a", "edge_b", "edge", "size_a", "size_b"]


def panel_all_days(db=DB, last_n: int | None = None) -> pd.DataFrame:
    """The per-second arbitrage panel, built one day at a time (only the needed columns are kept)."""
    from . import ticks
    c = ticks.connect(db)
    try:
        ranges = list(ticks.by_day(c, last_n))
    finally:
        c.close()
    parts = []
    for t0, t1, _, mk in ranges:
        d = panel(*load(db, t0, t1, mk))
        if len(d):
            parts.append(d[KEEP])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def panel(pm: pd.DataFrame, kb: pd.DataFrame, mk: pd.DataFrame) -> pd.DataFrame:
    """One row per second per window with both venues' top of book."""
    if pm.empty or kb.empty:
        return pd.DataFrame()
    mk = mk.copy()
    mk["start"] = pd.to_datetime(mk.close_ts - 15 * 60_000, unit="ms", utc=True)
    mk["slug"] = mk.start.dt.strftime("cpc-btc-updown-15m-%Y-%m-%d-") + mk.start.dt.strftime("%H%M") + "z"
    k = kb.assign(sec=kb.ts // 1000).groupby(["ticker", "sec"]).last().reset_index()
    k = k.merge(mk[["ticker", "slug", "close_ts", "target", "result", "expiration_value"]], on="ticker")
    p = pm.assign(sec=pm.ts // 1000).groupby(["slug", "sec"]).last().reset_index()
    d = k.merge(p, on=["slug", "sec"], suffixes=("_k", "_p")).dropna(subset=["yes_bid", "yes_ask", "best_bid", "best_ask"])
    if d.empty:
        return d
    d["secs_left"] = d.close_ts / 1000 - d.sec
    # A: Kalshi YES at ask + PM-US Down at (1 - Up bid)
    d["cost_a"] = d.yes_ask + (1 - d.best_bid)
    d["fees_a"] = d.yes_ask.map(lambda a: kalshi_fee(1, a)) + d.best_bid.map(lambda b: pmus_fee(1, b))
    d["size_a"] = np.minimum(d.yes_ask_sz, d.bid_sz)
    # B: PM-US Up at ask + Kalshi NO at (1 - YES bid)
    d["cost_b"] = d.best_ask + (1 - d.yes_bid)
    d["fees_b"] = d.best_ask.map(lambda a: pmus_fee(1, a)) + d.yes_bid.map(lambda b: kalshi_fee(1, 1 - b))
    d["size_b"] = np.minimum(d.ask_sz, d.yes_bid_sz)
    d["edge_a"] = 1 - d.cost_a - d.fees_a
    d["edge_b"] = 1 - d.cost_b - d.fees_b
    d["edge"] = d[["edge_a", "edge_b"]].max(axis=1)
    return d


def first_trades(d: pd.DataFrame, min_edge: float = 0.005, min_secs: float = 10) -> pd.DataFrame:
    """First second per window where either combination clears min_edge after fees (taker on both legs)."""
    if d.empty:
        return pd.DataFrame(columns=["ticker", "close_ts", "edge", "size", "pnl"])
    q = d[(d.edge > min_edge) & (d.secs_left >= min_secs)].sort_values("sec").drop_duplicates("ticker").copy()
    q["size"] = np.where(q.edge_a >= q.edge_b, q.size_a, q.size_b)
    # if both venues settle the same way the pair pays exactly $1; settlement divergence is tracked separately
    q["pnl"] = q.edge
    return q[["ticker", "close_ts", "edge", "size", "pnl"]]


def summary(d: pd.DataFrame) -> dict:
    if d.empty:
        return {"status": "no overlapping Kalshi + Polymarket US data yet"}
    w = d.groupby("ticker")
    near = d.drop_duplicates("ticker").dropna(subset=["expiration_value", "target"])
    return {
        "status": "ok",
        "windows": int(d.ticker.nunique()),
        "seconds": int(len(d)),
        "share_seconds_edge_gt_0": float((d.edge > 0).mean()),
        "share_windows_any_edge_gt_0": float(w.edge.max().gt(0).mean()),
        "share_windows_edge_gt_1c": float(w.edge.max().gt(0.01).mean()),
        "median_best_edge_per_window": float(w.edge.max().median()),
        "median_combined_cost_before_fees": float(d[["cost_a", "cost_b"]].min(axis=1).median()),
        # settlement-divergence exposure: windows that finished within $2 of the target
        "share_windows_settled_within_2usd": float(((near.expiration_value - near.target).abs() < 2).mean())
        if len(near) else None,
    }


def main() -> None:
    d = panel_all_days()
    for k, v in summary(d).items():
        print(f"{k}: {v}")
    t = first_trades(d)
    if len(t):
        print(f"\nwindows with a tradable gap (> 0.5c after fees, 10s+ left): {len(t)}; "
              f"mean edge {t.edge.mean() * 100:.2f}c, median size {t['size'].median():.0f} contracts")


if __name__ == "__main__":
    main()

"""Kalshi vs Polymarket US cross-venue arbitrage on the same 15-minute BTC window.

    uv run python -m pipeline.arb

Both venues settle on CF Benchmarks BRTI averaged over the final minute, so
"Up/YES on one venue + Down/NO on the other" pays exactly $1 if both settle the
same way. Two combinations, priced each second from the recorder's books:

  A: buy Kalshi YES at its ask       + buy PM-US Down (sell Up) at the Up bid
  B: buy PM-US Up at its ask         + buy Kalshi NO at (1 - Kalshi YES bid)

Edge = 1 - cost - Kalshi taker fee - PM-US taker fee (0.0695 * p * (1-p)).
Polymarket US averages [T-60s, T) while Kalshi's window includes the close
tick, so a near-tie can settle differently on the two venues and the pair then
pays $0 or $2. P&L is therefore realised from both venues' actual results
(Polymarket's from `pmus_results`), not assumed to be $1.

The recorder sees each book once a second, so a gap can be one venue's stale
quote. `first_trades(delay=1)` acts on a gap at second t but fills at second
t+1's prices, and only if the gap is still there.

    uv run python -m pipeline.arb --backfill-results   # fetch Polymarket US settlements for recorded windows
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
         mk: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Polymarket US books, Kalshi books, markets and Polymarket results for [t0, t1) ms (bounded)."""
    from . import ticks
    c = ticks.connect(db)
    try:
        t0 = 0 if t0 is None else t0
        t1 = 2**62 if t1 is None else t1
        mk = ticks.markets(c) if mk is None else mk
        return ticks.pmus_book(c, t0, t1), ticks.kalshi_book(c, t0, t1), \
            mk[["ticker", "close_ts", "target", "result", "expiration_value"]], ticks.pmus_results(c, t0, t1)
    finally:
        c.close()


KEEP = ["ticker", "sec", "close_ts", "secs_left", "target", "expiration_value", "result", "pm_up",
        "cost_a", "cost_b", "fees_a", "fees_b", "edge_a", "edge_b", "edge", "size_a", "size_b"]


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


def panel(pm: pd.DataFrame, kb: pd.DataFrame, mk: pd.DataFrame, res: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per second per window with both venues' top of book (and both results when known)."""
    if pm.empty or kb.empty:
        return pd.DataFrame()
    mk = mk.copy()
    mk["start"] = pd.to_datetime(mk.close_ts - 15 * 60_000, unit="ms", utc=True)
    mk["slug"] = mk.start.dt.strftime("cpc-btc-updown-15m-%Y-%m-%d-") + mk.start.dt.strftime("%H%M") + "z"
    res = res if res is not None and len(res) else pd.DataFrame(columns=["slug", "settle_px"])
    mk = mk.merge(res[["slug", "settle_px"]].rename(columns={"settle_px": "pm_up"}), on="slug", how="left")
    k = kb.assign(sec=kb.ts // 1000).groupby(["ticker", "sec"]).last().reset_index()
    k = k.merge(mk[["ticker", "slug", "close_ts", "target", "result", "expiration_value", "pm_up"]], on="ticker")
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


def payout(side: pd.Series, result: pd.Series, pm_up: pd.Series) -> pd.Series:
    """What the pair pays per contract: A = Kalshi YES + PM Down, B = PM Up + Kalshi NO. NaN if a result is missing."""
    k_yes = result.map({"yes": 1.0, "no": 0.0})
    pm_up = pd.to_numeric(pm_up, errors="coerce")
    return pd.Series(np.where(side == "A", k_yes + (1 - pm_up), pm_up + (1 - k_yes)), index=side.index)


def first_trades(d: pd.DataFrame, min_edge: float = 0.005, min_secs: float = 10, delay: int = 0) -> pd.DataFrame:
    """First second per window where either combination clears min_edge after fees (taker on both legs).

    delay=1 fills at the next second's prices and only if that same combination still clears min_edge.
    pnl is realised from both venues' results (NaN while Polymarket's is unknown); edge is the quoted edge."""
    cols = ["ticker", "close_ts", "side", "edge", "size", "payout", "pnl"]
    if d.empty:
        return pd.DataFrame(columns=cols)
    q = d[(d.edge > min_edge) & (d.secs_left >= min_secs)].copy()
    q["side"] = np.where(q.edge_a >= q.edge_b, "A", "B")
    fill = d[["ticker", "sec", "cost_a", "cost_b", "fees_a", "fees_b", "edge_a", "edge_b", "size_a", "size_b"]]
    if delay:
        q = q[["ticker", "sec", "close_ts", "side", "result", "pm_up"]].merge(
            fill.assign(sec=fill.sec - delay), on=["ticker", "sec"])
    a = q.side == "A"
    q["edge"] = np.where(a, q.edge_a, q.edge_b)
    q = q[q.edge > min_edge].sort_values("sec").drop_duplicates("ticker").copy()
    a = q.side == "A"
    q["size"] = np.where(a, q.size_a, q.size_b)
    q["payout"] = payout(q.side, q.result, q.pm_up) if "pm_up" in q else np.nan
    q["pnl"] = q.payout - np.where(a, q.cost_a + q.fees_a, q.cost_b + q.fees_b)
    return q[cols].reset_index(drop=True)


def persistence(d: pd.DataFrame, min_edge: float = 0.005, min_secs: float = 10) -> float | None:
    """Share of first gaps per window that are still there (same combination) one second later."""
    if d.empty:
        return None
    first = d[(d.edge > min_edge) & (d.secs_left >= min_secs)].sort_values("sec").drop_duplicates("ticker")
    if first.empty:
        return None
    nxt = d.assign(sec=d.sec - 1)[["ticker", "sec", "edge_a", "edge_b"]]
    m = first[["ticker", "sec", "edge_a", "edge_b"]].merge(nxt, on=["ticker", "sec"], how="left", suffixes=("", "_n"))
    return float((np.where(m.edge_a >= m.edge_b, m.edge_a_n, m.edge_b_n) > min_edge).mean())


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
        **splits(d),
    }


def splits(d: pd.DataFrame) -> dict:
    """How many windows have both results, and in how many the two venues settled differently."""
    w = d.drop_duplicates("ticker")
    both = w[w.result.isin(["yes", "no"]) & w.pm_up.notna()] if "pm_up" in w else w.iloc[0:0]
    split = (both.result.eq("yes") != (both.pm_up.astype(float) > 0.5)).sum() if len(both) else 0
    return {"windows_with_both_results": int(len(both)), "windows_settled_differently": int(split)}


def dollars(t: pd.DataFrame, bankroll: float = 100.0) -> float:
    """Total realised P&L if each pair is bought up to the visible size, capped at `bankroll` per trade."""
    t = t.dropna(subset=["pnl"])
    cost = (t.payout - t.pnl).clip(lower=0.01)  # cost + fees per pair
    return float((t.pnl * np.minimum(t["size"], bankroll / cost)).sum())


def backfill_results(db=DB) -> int:
    """Fetch Polymarket US settlements for every recorded window that has Polymarket books."""
    import time
    from datetime import datetime, timezone

    import httpx

    from recorder.run import PMUS, pmus_settle, pmus_slug
    c = sqlite3.connect(db, timeout=120)
    c.execute("CREATE TABLE IF NOT EXISTS pmus_results (slug TEXT PRIMARY KEY, close_ts INTEGER, state TEXT, "
              "settle_px REAL)")
    first = c.execute("SELECT min(ts) FROM pmus_book").fetchone()[0]
    if first is None:
        return 0
    todo = [r[0] for r in c.execute(
        "SELECT close_ts FROM kalshi_markets WHERE close_ts > ? AND close_ts < ? AND close_ts NOT IN "
        "(SELECT close_ts FROM pmus_results WHERE settle_px IS NOT NULL) ORDER BY close_ts",
        (first, int(time.time() * 1000) - 60_000))]
    n = 0
    with httpx.Client(timeout=10, headers={"User-Agent": "btc-predict-recorder"}) as h:
        for i, close_ts in enumerate(todo):
            slug = pmus_slug(datetime.fromtimestamp(close_ts / 1000 - 900, tz=timezone.utc))
            r = h.get(f"{PMUS}/{slug}/book")
            if r.status_code == 200 and (md := r.json().get("marketData")) and (px := pmus_settle(md)) is not None:
                c.execute("INSERT OR REPLACE INTO pmus_results VALUES (?,?,?,?)", (slug, close_ts, md.get("state"), px))
                n += 1
            if i % 100 == 99:
                c.commit()
                print(f"{i + 1}/{len(todo)} checked, {n} settled", flush=True)
            time.sleep(0.1)
    c.commit()
    c.close()
    return n


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-results", action="store_true", help="fetch Polymarket US settlements first")
    if ap.parse_args().backfill_results:
        print(f"Polymarket US results stored: {backfill_results()}")
    d = panel_all_days()
    for k, v in summary(d).items():
        print(f"{k}: {v}")
    p = persistence(d)
    if p is not None:
        print(f"share of first gaps still there 1 s later: {p:.0%}")
    for delay in (0, 1):
        t = first_trades(d, delay=delay)
        r = t.dropna(subset=["pnl"])
        if len(t):
            print(f"\n{'no delay' if delay == 0 else '1 s delay'}: {len(t)} windows traded, quoted edge "
                  f"{t.edge.mean() * 100:.2f}c, median size {t['size'].median():.0f} contracts")
            if len(r):
                print(f"  realised on {len(r)} windows with both results: {r.pnl.mean() * 100:+.2f}c per contract, "
                      f"{(r.payout != 1).sum()} split settlements, ${dollars(r):+,.2f} at up to $100 per trade")


if __name__ == "__main__":
    main()

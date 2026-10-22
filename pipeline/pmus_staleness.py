"""How stale is Polymarket US's public REST book compared with its real-time WebSocket?

    uv run python -m pipeline.pmus_staleness [--days 3]

The recorder keeps both: `pmus_book` (the REST copy the gateway serves from a 30 s cache) and `pmus_ws` (the real-time
feed). For every REST snapshot this finds the real-time book standing at the same instant and compares the two.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from . import ticks
from .config import DATA_DIR

DB = DATA_DIR / "ticks.sqlite"
OPEN = "MARKET_STATE_OPEN"


def compare(rest: pd.DataFrame, live: pd.DataFrame) -> dict:
    """rest: ts, slug, best_bid, best_ask (cached copy); live: ts, slug, state, best_bid, best_ask (real time)."""
    if rest.empty or live.empty:
        return {"status": "need both the REST snapshots (pmus_book) and the real-time feed (pmus_ws) recorded together"}
    r = rest.dropna(subset=["best_bid", "best_ask"]).sort_values("ts")
    p = live.rename(columns={"ts": "live_ts", "best_bid": "live_bid", "best_ask": "live_ask"}).sort_values("live_ts")
    d = pd.merge_asof(r, p[["live_ts", "slug", "state", "live_bid", "live_ask"]], left_on="ts", right_on="live_ts",
                      by="slug", direction="backward", tolerance=120_000)
    d = d.dropna(subset=["live_bid", "live_ask"])
    d = d[d.state.isna() | (d.state == OPEN)]
    if d.empty:
        return {"status": "the two feeds never overlap in time for the same window"}
    mid_diff = ((d.best_bid + d.best_ask) / 2 - (d.live_bid + d.live_ask) / 2).abs() * 100   # cents
    same = (d.best_bid == d.live_bid) & (d.best_ask == d.live_ask)
    # how long ago the real-time book last showed the cached prices: 0 if it still does
    return {
        "status": "ok",
        "snapshots_compared": int(len(d)),
        "windows": int(d.slug.nunique()),
        "share_identical_to_real_time": float(same.mean()),
        "mean_abs_mid_difference_cents": float(mid_diff.mean()),
        "median_abs_mid_difference_cents": float(mid_diff.median()),
        "p90_abs_mid_difference_cents": float(np.percentile(mid_diff, 90)),
        "share_off_by_2c_or_more": float((mid_diff >= 2).mean()),
        "share_off_by_5c_or_more": float((mid_diff >= 5).mean()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    c = ticks.connect(DB)
    try:
        if not ticks.has_table(c, "pmus_ws"):
            print("no real-time Polymarket data recorded yet")
            return
        hi = c.execute("SELECT max(ts) FROM pmus_ws").fetchone()[0]
        if hi is None:
            print("no real-time Polymarket data recorded yet")
            return
        lo = hi - a.days * 86_400_000
        rest = pd.read_sql("SELECT ts, slug, best_bid, best_ask FROM pmus_book WHERE ts >= ? AND ts < ?", c,
                           params=(lo, hi)) if ticks.has_table(c, "pmus_book") else pd.DataFrame()
        live = ticks.pmus_live(c, lo - 900_000, hi)
    finally:
        c.close()
    for k, v in compare(rest, live).items():
        print(f"{k}: {v:.3f}" if isinstance(v, float) else f"{k}: {v}")


if __name__ == "__main__":
    main()

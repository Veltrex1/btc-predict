"""Second-level microstructure studies on the recorder's data (data/ticks.sqlite).

    uv run python -m pipeline.micro            # all studies on everything recorded so far

1. lead-lag   After BTC moves on spot venues, how long until Kalshi's mid catches up,
              and how often is the stale gap bigger than a taker's cost (half spread + fee)?
2. last-min   Kalshi prices in the final 60 seconds vs outcomes: is the market under-
              confident about a settlement average that is mostly locked in?
3. open       First 60 seconds: does Kalshi's mid reflect where spot already is vs the target?

Fair value uses the Brownian model with the settlement average's reduced variance,
so these studies don't depend on any trained model.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
from scipy.special import ndtr

from .config import DATA_DIR
from .strategy import fee

DB = DATA_DIR / "ticks.sqlite"


def load(db=DB) -> dict[str, pd.DataFrame]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    spot = pd.read_sql("SELECT ts, venue, bid, ask FROM spot WHERE bid IS NOT NULL AND ask IS NOT NULL", c)
    book = pd.read_sql("SELECT ts, ticker, yes_bid, yes_ask, yes_bid_sz, yes_ask_sz FROM kalshi_book "
                       "WHERE yes_bid IS NOT NULL AND yes_ask IS NOT NULL", c)
    mk = pd.read_sql("SELECT * FROM kalshi_markets", c)
    c.close()
    return {"spot": spot, "book": book, "markets": mk}


def spot_mid_1s(spot: pd.DataFrame) -> pd.Series:
    """Composite spot mid per second: median across venues of each venue's last mid in that second."""
    s = spot.assign(mid=(spot.bid + spot.ask) / 2, sec=spot.ts // 1000)
    per = s.groupby(["sec", "venue"])["mid"].last().unstack().sort_index().ffill(limit=5)
    return per.median(axis=1)


def fair_value(mid: np.ndarray, target: float, secs_left: np.ndarray, sigma_1s: float) -> np.ndarray:
    """P(60s settlement average >= target) under Brownian motion from the current spot mid."""
    t = np.maximum(secs_left, 0.0)
    # variance of the remaining path entering the average: full for t>60, partially locked inside the last minute
    var = np.where(t > 60, (t - 60) + 20.0, (t ** 3) / (3 * 60 ** 2))
    sd = sigma_1s * mid * np.sqrt(np.maximum(var, 1e-6))
    return ndtr((mid - target) / sd)


def build_panel(d: dict) -> pd.DataFrame:
    """One row per second per market: Kalshi mid, spot mid, fair value, seconds left, outcome."""
    smid = spot_mid_1s(d["spot"])
    lr = np.log(smid).diff().dropna()
    sigma_1s = float(lr.tail(3600).std()) if len(lr) > 60 else 2e-4
    b = d["book"].assign(sec=d["book"].ts // 1000)
    b = b.groupby(["ticker", "sec"]).last().reset_index()
    m = d["markets"].dropna(subset=["target"])
    b = b.merge(m[["ticker", "close_ts", "target", "result"]], on="ticker")
    b["spot"] = smid.reindex(b["sec"]).to_numpy()
    b = b.dropna(subset=["spot"])
    b["secs_left"] = b["close_ts"] / 1000 - b["sec"]
    b["mid"] = (b.yes_bid + b.yes_ask) / 2
    b["fair"] = fair_value(b["spot"].to_numpy(), b["target"].to_numpy(), b["secs_left"].to_numpy(), sigma_1s)
    b["y"] = (b["result"] == "yes").astype(float).where(b["result"].notna())
    b.attrs["sigma_1s"] = sigma_1s
    return b


def lead_lag(p: pd.DataFrame, max_lag: int = 15) -> pd.DataFrame:
    """Correlation of Kalshi mid changes at t+k with fair-value changes at t (per market, pooled)."""
    out = []
    for k in range(0, max_lag + 1):
        xs, ys = [], []
        for _, g in p.groupby("ticker"):
            g = g.set_index("sec").sort_index()
            g = g.reindex(range(g.index.min(), g.index.max() + 1)).ffill()
            df, dm = g["fair"].diff(), g["mid"].diff().shift(-k)
            ok = df.notna() & dm.notna() & (g["secs_left"] > 60)
            xs.append(df[ok]); ys.append(dm[ok])
        x, y = pd.concat(xs), pd.concat(ys)
        out.append({"lag_s": k, "corr": float(np.corrcoef(x, y)[0, 1]) if len(x) > 30 else np.nan, "n": len(x)})
    return pd.DataFrame(out)


def stale_gaps(p: pd.DataFrame) -> pd.DataFrame:
    """How often fair value sits beyond the taker's all-in cost, and what that trade earned."""
    q = p[(p.secs_left > 20) & p.y.notna()].copy()
    cost_yes = q.yes_ask + [fee(1, a) for a in q.yes_ask]
    cost_no = (1 - q.yes_bid) + [fee(1, 1 - b) for b in q.yes_bid]
    q["edge_yes"] = q.fair - cost_yes
    q["edge_no"] = (1 - q.fair) - cost_no
    rows = []
    for thr in (0.0, 0.02, 0.05, 0.10):
        buy_yes = q[q.edge_yes > thr]
        buy_no = q[q.edge_no > thr]
        pnl = pd.concat([buy_yes.y - cost_yes[buy_yes.index], (1 - buy_no.y) - cost_no[buy_no.index]])
        rows.append({"edge_over": thr, "seconds": len(pnl), "share_of_time": len(pnl) / max(len(q), 1),
                     "markets": pd.concat([buy_yes.ticker, buy_no.ticker]).nunique(),
                     "avg_realized_pnl": float(pnl.mean()) if len(pnl) else np.nan})
    return pd.DataFrame(rows)


def by_phase(p: pd.DataFrame) -> pd.DataFrame:
    q = p[p.y.notna()].copy()
    q["phase"] = pd.cut(q.secs_left, [0, 20, 60, 120, 840, 900], labels=["last 20s", "20-60s", "1-2m", "mid", "first 60s"])
    return q.groupby("phase", observed=True).apply(lambda g: pd.Series({
        "markets": g.ticker.nunique(), "seconds": len(g),
        "brier_market": ((g.mid - g.y) ** 2).mean(), "brier_fair": ((g.fair - g.y) ** 2).mean(),
        "mean_abs_gap": (g.fair - g.mid).abs().mean()}), include_groups=False)


def main() -> None:
    d = load()
    p = build_panel(d)
    settled = p[p.y.notna()].ticker.nunique()
    print(f"panel: {len(p):,} market-seconds, {p.ticker.nunique()} markets ({settled} settled), "
          f"spot sigma {p.attrs['sigma_1s']:.2e}/s")
    pd.set_option("display.width", 200)
    print("\nLead-lag (corr of Kalshi mid change at t+k with fair-value change at t):")
    print(lead_lag(p).round(4).to_string(index=False))
    if settled:
        print("\nForecast quality by phase (lower Brier is better):")
        print(by_phase(p).round(4).to_string())
        print("\nTaker trades when fair value beats ask + fee by a margin (realized, settled markets only):")
        print(stale_gaps(p).round(4).to_string(index=False))


if __name__ == "__main__":
    main()

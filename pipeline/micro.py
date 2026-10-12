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


# ── final minute: the settlement average is partly locked in ─────────────────────────
def settle_fair(value: np.ndarray, partial_avg: np.ndarray, n_done: np.ndarray, target: np.ndarray,
                secs_left: np.ndarray, sigma_usd_1s: float) -> np.ndarray:
    """P(final 60-tick BRTI average >= target).

    Inside the final minute, n_done ticks are already averaged into partial_avg and the
    remaining m = 60 - n_done ticks follow a random walk from the current value, so
    final = (n_done*partial + m*value + sum of m random-walk steps) / 60 with
    sd = sigma * sqrt(m(m+1)(2m+1)/6) / 60. Before the final minute, partial is unused and
    the variance is (secs_left - 60) + 61*121/(6*60) ticks of sigma^2."""
    n = np.clip(np.nan_to_num(n_done, nan=0.0), 0, 60)
    inside = (secs_left <= 60) & (n > 0)
    m = np.where(inside, 60 - n, 60.0)
    mean = np.where(inside, (n * np.nan_to_num(partial_avg) + m * value) / 60.0, value)
    var_inside = m * (m + 1) * (2 * m + 1) / 6.0 / 3600.0
    var_before = np.maximum(secs_left - 60, 0) + 61 * 121 / 360.0
    sd = sigma_usd_1s * np.sqrt(np.where(inside, var_inside, var_before))
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (mean - target) / np.where(sd > 0, sd, np.nan)
    return np.where(sd > 0, ndtr(z), (mean >= target).astype(float))


def load_brti(db=DB) -> pd.DataFrame:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        b = pd.read_sql("SELECT source_ts, value, settle_avg, settle_n FROM brti WHERE feed = '1hz' AND value IS NOT NULL", c)
    except Exception:  # noqa: BLE001 - table absent until the API key is configured
        b = pd.DataFrame(columns=["source_ts", "value", "settle_avg", "settle_n"])
    c.close()
    return b


def final_minute_trades(d: dict, brti: pd.DataFrame, max_secs: float = 60, min_secs: float = 3,
                        thr: float = 0.02) -> pd.DataFrame:
    """First taker trade per settled market when settlement-aware fair value beats ask + fee by `thr`
    with between min_secs and max_secs left. Returns ticker, close_ts, side, cost, pnl."""
    if brti.empty:
        return pd.DataFrame(columns=["ticker", "close_ts", "side", "cost", "pnl"])
    br = brti.assign(sec=brti.source_ts // 1000).groupby("sec").last()
    sigma = float(br["value"].diff().tail(3600).std())
    b = d["book"].assign(sec=d["book"].ts // 1000).groupby(["ticker", "sec"]).last().reset_index()
    m = d["markets"].dropna(subset=["target", "result"])
    b = b.merge(m[["ticker", "close_ts", "target", "result"]], on="ticker")
    b["secs_left"] = b.close_ts / 1000 - b.sec
    b = b[(b.secs_left >= min_secs) & (b.secs_left <= max_secs)].join(br, on="sec", how="inner")
    if b.empty:
        return pd.DataFrame(columns=["ticker", "close_ts", "side", "cost", "pnl"])
    b["fair"] = settle_fair(b.value.to_numpy(), b.settle_avg.to_numpy(), b.settle_n.to_numpy(),
                            b.target.to_numpy(), b.secs_left.to_numpy(), sigma)
    b["cy"] = b.yes_ask + b.yes_ask.map(lambda a: fee(1, a))
    b["cn"] = (1 - b.yes_bid) + b.yes_bid.map(lambda x: fee(1, 1 - x))
    yes = b[b.fair - b.cy > thr].assign(side="yes", cost=lambda x: x.cy)
    no = b[(1 - b.fair) - b.cn > thr].assign(side="no", cost=lambda x: x.cn)
    first = pd.concat([yes, no]).sort_values("sec").drop_duplicates("ticker")
    first["pnl"] = np.where(first.side == first.result, 1.0, 0.0) - first.cost
    return first[["ticker", "close_ts", "side", "cost", "pnl"]]


def final_minute(d: dict, brti: pd.DataFrame) -> dict:
    """Kalshi quotes in the last 90 s vs settlement-aware fair value; realized P&L of taking the edge."""
    if brti.empty:
        return {"status": "no BRTI data yet (needs the Kalshi API key on the recorder)"}
    br = brti.assign(sec=brti.source_ts // 1000).groupby("sec").last()
    sigma = float(br["value"].diff().tail(3600).std())
    b = d["book"].assign(sec=d["book"].ts // 1000).groupby(["ticker", "sec"]).last().reset_index()
    m = d["markets"].dropna(subset=["target", "result"])
    b = b.merge(m[["ticker", "close_ts", "target", "result", "expiration_value"]], on="ticker")
    b["secs_left"] = b.close_ts / 1000 - b.sec
    b = b[(b.secs_left > 0) & (b.secs_left <= 90)].join(br, on="sec", how="inner")
    if b.empty:
        return {"status": "no overlapping final-minute data yet"}
    b["fair"] = settle_fair(b.value.to_numpy(), b.settle_avg.to_numpy(), b.settle_n.to_numpy(),
                            b.target.to_numpy(), b.secs_left.to_numpy(), sigma)
    b["y"] = (b.result == "yes").astype(float)
    b["ey"] = b.fair - b.yes_ask - b.yes_ask.map(lambda a: fee(1, a))
    b["en"] = (1 - b.fair) - (1 - b.yes_bid) - b.yes_bid.map(lambda x: fee(1, 1 - x))
    # settlement check: our last full average vs Kalshi's official expiration value
    last = br[br.settle_n == 60].settle_avg
    chk = m.assign(sec=(m.close_ts // 1000)).join(last.rename("our_avg"), on="sec").dropna(subset=["our_avg", "expiration_value"])
    rows = []
    for lo, hi in ((60, 90), (40, 60), (20, 40), (5, 20)):
        g = b[(b.secs_left > lo) & (b.secs_left <= hi)] if lo else b[b.secs_left <= hi]
        for thr in (0.0, 0.02):
            yes, no = g[g.ey > thr], g[g.en > thr]
            first = pd.concat([yes.assign(side="yes"), no.assign(side="no")]).sort_values("sec").drop_duplicates("ticker")
            cost = np.where(first.side == "yes", first.yes_ask + first.yes_ask.map(lambda a: fee(1, a)),
                            (1 - first.yes_bid) + first.yes_bid.map(lambda x: fee(1, 1 - x)))
            win = np.where(first.side == "yes", first.y, 1 - first.y)
            rows.append({"secs_left": f"{lo}-{hi}", "edge_over": thr, "markets_traded": len(first),
                         "markets_seen": g.ticker.nunique(), "avg_pnl": float(np.mean(win - cost)) if len(first) else None})
    return {"status": "ok", "sigma_usd_1s": sigma, "markets": int(b.ticker.nunique()),
            "brier_market": float((((b.yes_bid + b.yes_ask) / 2 - b.y) ** 2).mean()),
            "brier_fair": float(((b.fair - b.y) ** 2).mean()),
            "settle_check_n": int(len(chk)),
            "settle_check_max_abs_diff": float((chk.our_avg - chk.expiration_value).abs().max()) if len(chk) else None,
            "table": pd.DataFrame(rows)}


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
    fm = final_minute(d, load_brti())
    print("\nFinal minute (settlement-average-aware fair value):")
    for k, v in fm.items():
        print(v.round(4).to_string(index=False) if isinstance(v, pd.DataFrame) else f"  {k}: {v}")


if __name__ == "__main__":
    main()

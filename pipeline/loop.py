"""The learning loop: keep data current, re-test every candidate edge out of sample,
and promote only what keeps working.

    uv run python -m pipeline.loop --once          # one cycle now
    uv run python -m pipeline.loop --every 6       # forever, every 6 hours (the droplet runs this)

Each cycle:
  1. tops up Kalshi markets and minute quotes (batched) and recent Coinbase bars;
  2. downloads trade history for more settled windows (maker-fill study);
  3. runs every candidate study and scores it on the most recent data it was NOT designed on;
  4. appends results to data/loop_history.jsonl and writes reports/loop_latest.md.

Promotion rule (the only way a strategy reaches shadow and then live trading):
  positive mean P&L per contract after fees on BOTH the latest 2 weeks and the 2 weeks before,
  with at least 300 independent windows and a t-statistic >= 2 on the combined sample.
"""
from __future__ import annotations

import argparse
import json
import time
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import db
from .config import DATA_DIR, REPORT_DIR

HISTORY = DATA_DIR / "loop_history.jsonl"
LATEST = REPORT_DIR / "loop_latest.md"


def score(pnl: pd.Series, when: pd.Series, label: str) -> dict:
    """Out-of-sample scoring on per-window P&L: last 14 days vs the 14 days before."""
    df = pd.DataFrame({"pnl": pnl.to_numpy(), "t": pd.to_datetime(when).to_numpy()}).dropna()
    if df.empty:
        return {"candidate": label, "windows": 0, "promote": False}
    end = df.t.max()
    recent = df[df.t > end - pd.Timedelta(days=14)]
    prior = df[(df.t <= end - pd.Timedelta(days=14)) & (df.t > end - pd.Timedelta(days=28))]
    both = pd.concat([recent, prior])
    t_stat = both.pnl.mean() / (both.pnl.std() / np.sqrt(len(both))) if len(both) > 2 and both.pnl.std() > 0 else 0
    out = {"candidate": label, "windows": int(len(both)), "pnl_recent": float(recent.pnl.mean()) if len(recent) else None,
           "pnl_prior": float(prior.pnl.mean()) if len(prior) else None, "t_stat": float(t_stat)}
    out["promote"] = bool(len(both) >= 300 and t_stat >= 2 and (out["pnl_recent"] or 0) > 0 and (out["pnl_prior"] or 0) > 0)
    return out


# ── candidates ────────────────────────────────────────────────────────
def cand_taker_model() -> list[dict]:
    """Buy whichever side the calibrated model says beats ask + fee by 3c+, hold to settlement (minute quotes)."""
    from .strategy import fee
    wf = pd.read_parquet(DATA_DIR / "walkforward.parquet")
    wf = wf[(wf.label_source == "kalshi") & wf.ticker.notna()]
    con = db.connect(read_only=True)
    q = con.execute("SELECT ticker, ts, yes_bid, yes_ask FROM kalshi_candles WHERE yes_ask IS NOT NULL").df()
    con.close()
    d = wf.merge(q, left_on=["ticker", "at"], right_on=["ticker", "ts"])
    d = d[(d.yes_ask - d.yes_bid) <= 0.03]
    d["ey"] = d.p_gbm - d.yes_ask - d.yes_ask.map(lambda a: fee(1, a))
    d["en"] = (1 - d.p_gbm) - (1 - d.yes_bid) - d.yes_bid.map(lambda b: fee(1, 1 - b))
    first = d[(d.ey > 0.03) | (d.en > 0.03)].sort_values("at").drop_duplicates("ticker")
    yes = first.ey >= first.en
    cost = np.where(yes, first.yes_ask + first.yes_ask.map(lambda a: fee(1, a)),
                    (1 - first.yes_bid) + first.yes_bid.map(lambda b: fee(1, 1 - b)))
    win = np.where(yes, first.label, 1 - first.label)
    return [score(pd.Series(win - cost), first.window_open, "taker: model edge > 3c, hold")]


def cand_maker_fills() -> list[dict]:
    """Realized P&L of real maker fills (public trade history), by price band, contract-weighted per window."""
    from .maker_study import OUT, maker_fills
    if not OUT.exists():
        return []
    t = maker_fills(pd.read_parquet(OUT))
    t = t[(t.mins_left > 0.25) & (t.price > 0.02) & (t.price < 0.98)]
    out = []
    for lo, hi, tl, th in [(0.70, 0.90, 6, 14), (0.40, 0.60, 6, 14), (0.10, 0.30, 2, 5), (0.90, 0.98, 0.25, 2)]:
        g = t[(t.price >= lo) & (t.price < hi) & (t.mins_left >= tl) & (t.mins_left < th)]
        if g.empty:
            continue
        per = g.groupby("ticker").apply(lambda x: pd.Series({
            "pnl": np.average(x.pnl, weights=x["count"]), "t": x.close_time.iloc[0]}), include_groups=False)
        out.append(score(per.pnl, per.t, f"maker fills {lo:.2f}-{hi:.2f} at {tl}-{th} min left"))
    return out


def cand_micro() -> list[dict]:
    """Second-level: taker when spot-implied fair value beats ask + fee by 2c (recorder data)."""
    from . import micro
    if not micro.DB.exists():
        return []
    p = micro.build_panel(micro.load())
    from .strategy import fee
    q = p[(p.secs_left > 20) & p.y.notna()].copy()
    if q.empty:
        return []
    q["ey"] = q.fair - q.yes_ask - q.yes_ask.map(lambda a: fee(1, a))
    q["en"] = (1 - q.fair) - (1 - q.yes_bid) - q.yes_bid.map(lambda b: fee(1, 1 - b))
    first = q[(q.ey > 0.02) | (q.en > 0.02)].sort_values("sec").drop_duplicates("ticker")
    yes = first.ey >= first.en
    cost = np.where(yes, first.yes_ask + first.yes_ask.map(lambda a: fee(1, a)),
                    (1 - first.yes_bid) + first.yes_bid.map(lambda b: fee(1, 1 - b)))
    win = np.where(yes, first.y, 1 - first.y)
    return [score(pd.Series(win - cost), pd.to_datetime(first.close_ts, unit="ms"), "micro: spot fair value > ask+fee+2c")]


def cand_final_minute() -> list[dict]:
    """Taker in the last 60 s when the settlement-average-aware fair value beats ask + fee by 2c (needs BRTI stream)."""
    from . import micro
    if not micro.DB.exists():
        return []
    d, br = micro.load(), micro.load_brti()
    out = []
    for lo, hi in ((20, 60), (3, 20)):
        t = micro.final_minute_trades(d, br, max_secs=hi, min_secs=lo)
        if len(t):
            out.append(score(t.pnl, pd.to_datetime(t.close_ts, unit="ms"), f"final minute: taker {lo}-{hi}s left, edge > 2c"))
    return out


def cand_arb() -> list[dict]:
    """Kalshi vs Polymarket US: buy both sides when the pair costs < $1 after both taker fees."""
    from . import arb
    if not arb.DB.exists():
        return []
    t = arb.first_trades(arb.panel(*arb.load()))
    if t.empty:
        return []
    return [score(t.pnl, pd.to_datetime(t.close_ts, unit="ms"), "arb: Kalshi + Polymarket US pair < $1 after fees")]


CANDIDATES = [cand_taker_model, cand_maker_fills, cand_micro, cand_final_minute, cand_arb]


# ── data top-up ───────────────────────────────────────────────────────
def refresh_data(maker_windows: int) -> None:
    from . import backfill_coinbase, backfill_kalshi, maker_study
    for name, fn in [("kalshi", lambda: backfill_kalshi.run(candles=True)),
                     ("coinbase", lambda: backfill_coinbase.run(days=3)),
                     ("maker trades", lambda: maker_study.download(maker_windows, "2026-08-12"))]:
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"refresh {name} failed:\n{traceback.format_exc()}")


def cycle(refresh: bool, maker_windows: int) -> list[dict]:
    if refresh:
        refresh_data(maker_windows)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for c in CANDIDATES:
        try:
            for r in c():
                results.append({"ts": stamp, **r})
        except Exception:  # noqa: BLE001
            results.append({"ts": stamp, "candidate": c.__name__, "error": traceback.format_exc()[-500:]})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY, "a") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"# Learning loop, {stamp}", "",
             "| candidate | windows | P&L/contract last 14d | prior 14d | t-stat | promote |",
             "|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['candidate']} | error | | | | |")
            continue
        f = lambda v: "" if v is None else f"{v * 100:+.2f}¢"  # noqa: E731
        lines.append(f"| {r['candidate']} | {r['windows']} | {f(r.get('pnl_recent'))} | {f(r.get('pnl_prior'))} | "
                     f"{r.get('t_stat', 0):.2f} | {'YES' if r.get('promote') else 'no'} |")
    LATEST.write_text("\n".join(lines) + "\n")
    (DATA_DIR / "loop_latest.json").write_text(json.dumps({"ts": stamp, "results": results}, default=str))
    print("\n".join(lines))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=float, default=6.0, help="hours between cycles")
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--maker-windows", type=int, default=150, help="new windows of trade history per cycle")
    args = ap.parse_args()
    while True:
        cycle(not args.no_refresh, args.maker_windows)
        if args.once:
            return
        time.sleep(args.every * 3600)


if __name__ == "__main__":
    main()

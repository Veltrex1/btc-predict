"""Replay the swing strategy over real Kalshi quote history.

    uv run python -m pipeline.replay                 # tune on early months, report the last 3
    uv run python -m pipeline.replay --test-months 3 --p p_gbm

Inputs: out-of-sample probabilities from train.py (data/walkforward.parquet) and
Kalshi 1-minute YES bid/ask candles (kalshi_candles). At each minute of each
window the strategy sees that minute's closing quote and model probability;
buys fill at the ask, sells at the bid, every fill pays Kalshi's taker fee, and
anything still held settles on Kalshi's official result.

Limits, stated in the report: quotes are 1-minute snapshots (live trading acts
every second), there is no order-book depth (assumes our 1-10 contracts fill),
and no latency.
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from . import db
from .config import DATA_DIR, ROOT
from .evaluate import WF_PATH
from .strategy import Params, Position, Quote, decide, fee

STRATEGY_PATH = ROOT / "models" / "strategy.json"


def load_inputs(p_col: str) -> pd.DataFrame:
    preds = pd.read_parquet(WF_PATH)
    preds = preds[(preds["label_source"] == "kalshi") & preds["ticker"].notna() & (preds["minute"] >= 1)]
    con = db.connect(read_only=True)
    q = con.execute("SELECT ticker, ts, yes_bid, yes_ask FROM kalshi_candles").df()
    df = preds[["window_open", "minute", "at", "secs_left", "label", "ticker", p_col]].merge(
        q, left_on=["ticker", "at"], right_on=["ticker", "ts"], how="left").drop(columns=["ts"])
    df = df.sort_values(["window_open", "minute"])
    # a minute with no candle means the quote did not change: carry the last one forward within the window
    df[["yes_bid", "yes_ask"]] = df.groupby("window_open")[["yes_bid", "yes_ask"]].ffill()
    return df.rename(columns={p_col: "p"}).reset_index(drop=True)


def run_window(g: pd.DataFrame, params: Params, equity: float) -> tuple[float, list[dict]]:
    """Simulate one window. Returns (pnl, trades)."""
    pos: Position | None = None
    cash = 0.0
    entries = 0
    trades = []
    for r in g.itertuples(index=False):
        if pd.isna(r.yes_bid) or pd.isna(r.yes_ask):
            continue
        q = Quote(float(r.yes_bid), float(r.yes_ask))
        a = decide(pos, float(r.p), q, float(r.secs_left), equity + cash, entries, params)
        if a is None:
            continue
        f = fee(a.count, a.price)
        if a.kind == "buy":
            cash -= a.count * a.price + f
            pos = Position(a.side, a.count, a.price, f)
            entries += 1
            trades.append({"window_open": r.window_open, "minute": r.minute, "kind": "buy", "side": a.side,
                           "count": a.count, "price": a.price, "fee": f})
        else:
            cash += a.count * a.price - f
            trades.append({"window_open": r.window_open, "minute": r.minute, "kind": "sell", "side": a.side,
                           "count": a.count, "price": a.price, "fee": f, "reason": a.reason})
            pos = None
    if pos is not None:
        label = int(g["label"].iloc[0])
        won = (pos.side == "yes") == (label == 1)
        cash += pos.count * (1.0 if won else 0.0)
        trades.append({"window_open": g["window_open"].iloc[0], "minute": 15, "kind": "settle", "side": pos.side,
                       "count": pos.count, "price": 1.0 if won else 0.0, "fee": 0.0})
    return cash, trades


def simulate(df: pd.DataFrame, params: Params, equity: float = 10.0) -> pd.DataFrame:
    """Every window independently with a fixed $10 equity: per-window P&L."""
    out = []
    for w, g in df.groupby("window_open", sort=True):
        pnl, trades = run_window(g, params, equity)
        if trades:
            out.append({"window_open": w, "pnl": pnl, "fills": sum(t["kind"] != "settle" for t in trades),
                        "fees": sum(t["fee"] for t in trades),
                        "entries": sum(t["kind"] == "buy" for t in trades),
                        "settled": any(t["kind"] == "settle" for t in trades)})
    return pd.DataFrame(out)


def daily_runs(df: pd.DataFrame, params: Params, start: float = 10.0, stop_loss: float = 5.0) -> pd.DataFrame:
    """What the user will actually do: start each UTC day with $10, 10% sizing, halt at -$5."""
    rows = []
    for day, d in df.groupby(df["window_open"].dt.floor("D")):
        equity, trades, halted, low = start, 0, False, start
        for _, g in d.groupby("window_open", sort=True):
            pnl, t = run_window(g, params, equity)
            equity += pnl
            trades += sum(x["kind"] == "buy" for x in t)
            low = min(low, equity)
            if equity <= start - stop_loss:
                halted = True
                break
        rows.append({"day": day.date(), "end_equity": equity, "pnl": equity - start, "trades": trades,
                     "halted": halted, "low": low})
    return pd.DataFrame(rows)


def summarize(sim: pd.DataFrame, n_windows: int) -> dict:
    if sim.empty:
        return {"windows_traded": 0, "total_pnl": 0.0}
    days = max(1, sim["window_open"].dt.floor("D").nunique())
    return {
        "windows": n_windows,
        "windows_traded": int(len(sim)),
        "entries": int(sim["entries"].sum()),
        "win_rate_windows": float((sim["pnl"] > 0).mean()),
        "total_pnl": float(sim["pnl"].sum()),
        "pnl_per_day": float(sim["pnl"].sum() / days),
        "pnl_per_window_traded": float(sim["pnl"].mean()),
        "fees": float(sim["fees"].sum()),
        "share_held_to_settlement": float(sim["settled"].mean()),
        "worst_window": float(sim["pnl"].min()),
    }


GRID = {
    "entry_edge": [0.02, 0.04, 0.06, 0.08, 0.12],
    "take_profit": [0.02, 0.04, 0.08, 9.0],      # 9.0 = never take profit early
    "exit_margin": [0.0, 0.03, 9.0],            # 9.0 = never exit on fair value
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--p", default=None, help="probability column (default: the model train.py chose)")
    ap.add_argument("--test-months", type=int, default=3)
    args = ap.parse_args()

    from .model import Predictor
    p_col = args.p or ("p_gbm" if Predictor.load().kind == "gbm" else "p_bm")
    df = load_inputs(p_col)
    month = df["window_open"].dt.to_period("M")
    months = sorted(month.unique())
    test_m = months[-args.test_months:]
    tune = df[~month.isin(test_m)]
    test = df[month.isin(test_m)]
    print(f"p={p_col}; quotes on {df['yes_ask'].notna().mean():.1%} of minutes; "
          f"tune {tune['window_open'].nunique():,} windows ({months[0]}–{months[-args.test_months - 1]}), "
          f"test {test['window_open'].nunique():,} windows ({test_m[0]}–{test_m[-1]})")

    results = []
    for ee, tp, em in itertools.product(*GRID.values()):
        params = replace(Params(), entry_edge=ee, take_profit=tp, exit_margin=em)
        s = summarize(simulate(tune, params), tune["window_open"].nunique())
        results.append({"entry_edge": ee, "take_profit": tp, "exit_margin": em, **s})
        print(f"  ee={ee:.2f} tp={tp:.2f} em={em:.2f}  pnl/day ${s['pnl_per_day']:+.3f}  "
              f"entries {s.get('entries', 0)}", flush=True)
    grid = pd.DataFrame(results).sort_values("pnl_per_day", ascending=False)
    best = grid.iloc[0]
    params = replace(Params(), entry_edge=float(best["entry_edge"]), take_profit=float(best["take_profit"]),
                     exit_margin=float(best["exit_margin"]))

    test_sim = simulate(test, params)
    test_sum = summarize(test_sim, test["window_open"].nunique())
    days = daily_runs(test, params)
    hold_only = summarize(simulate(test, replace(params, take_profit=9.0, exit_margin=9.0)),
                          test["window_open"].nunique())

    pd.set_option("display.width", 200, "display.max_columns", 30)
    print("\nTop 10 parameter sets on the TUNING months:")
    print(grid.head(10).round(4).to_string(index=False))
    print(f"\nChosen: {asdict(params)}")
    print("\nTEST months (never used for tuning), $10 equity, 10% sizing, per window:")
    print(json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in test_sum.items()}, indent=2))
    print("\nSame entries but always holding to settlement (no swing exits):")
    print(json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in hold_only.items()}, indent=2))
    print("\n24-hour runs on test days ($10 start, 10% per trade, stop at -$5):")
    print(days.describe().round(3).to_string())
    print(f"days profitable: {(days['pnl'] > 0).mean():.1%}; days halted at -$5: {days['halted'].mean():.1%}; "
          f"median day ${days['pnl'].median():+.2f}")

    STRATEGY_PATH.parent.mkdir(parents=True, exist_ok=True)
    STRATEGY_PATH.write_text(json.dumps({"p_col": p_col, "params": asdict(params), "test": test_sum,
                                         "hold_only": hold_only,
                                         "daily": {"profitable_share": float((days["pnl"] > 0).mean()),
                                                   "halted_share": float(days["halted"].mean()),
                                                   "median_pnl": float(days["pnl"].median()),
                                                   "mean_pnl": float(days["pnl"].mean())}}, indent=2))
    grid.to_csv(DATA_DIR / "replay_grid.csv", index=False)
    days.to_csv(DATA_DIR / "replay_days.csv", index=False)
    print(f"\nsaved {STRATEGY_PATH}")


if __name__ == "__main__":
    main()

"""Evaluation on walk-forward (out-of-sample) predictions.

    uv run python -m pipeline.evaluate                 # uses data/walkforward.parquet from train.py
    uv run python -m pipeline.evaluate --tau 0.85 --lead 120

Per test month: accuracy on calls with a Wilson 95% CI, coverage (share of
windows called), when calls lock, Brier score, and, where Kalshi candles exist,
what the call would have cost at the ask and its expected value after fees,
plus how often simply following the Kalshi favourite was right on the same windows.
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

from . import db
from .config import DATA_DIR
from .kalshi import fee_per_contract
from .policy import grid, simulate, summarize

WF_PATH = DATA_DIR / "walkforward.parquet"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def brier(p, y) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2)) if len(p) else float("nan")


def attach_market(calls: pd.DataFrame, candles: pd.DataFrame | None) -> pd.DataFrame:
    """Kalshi quote at the lock moment (the 1-minute candle ending at `at`)."""
    calls = calls.copy()
    for c in ("yes_bid", "yes_ask"):
        calls[c] = np.nan
    if candles is None or candles.empty or "ticker" not in calls or calls["ticker"].isna().all():
        return calls
    q = candles.dropna(subset=["yes_ask"])[["ticker", "ts", "yes_bid", "yes_ask"]]
    merged = calls.drop(columns=["yes_bid", "yes_ask"]).merge(
        q, left_on=["ticker", "at"], right_on=["ticker", "ts"], how="left").drop(columns=["ts"])
    return merged


def market_metrics(calls: pd.DataFrame) -> dict:
    m = calls.dropna(subset=["yes_ask", "yes_bid"])
    if m.empty:
        return {"market_calls": 0}
    above = m["side"] == "ABOVE"
    cost = np.where(above, m["yes_ask"], 1 - m["yes_bid"])  # NO ask = 1 - YES bid
    fees = np.array([fee_per_contract(c) for c in cost])
    pnl = m["correct"].to_numpy() - cost - fees
    mid = (m["yes_ask"] + m["yes_bid"]) / 2
    fav_right = ((mid >= 0.5).astype(int) == m["label"]).mean()
    return {
        "market_calls": int(len(m)),
        "avg_cost": float(cost.mean()),
        "avg_fee": float(fees.mean()),
        "ev_per_contract": float(pnl.mean()),
        "total_pnl_1_contract": float(pnl.sum()),
        "market_favourite_accuracy": float(fav_right),
    }


def monthly_table(preds: pd.DataFrame, p_col: str, tau: float, lead: float,
                  candles: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = []
    for month, g in preds.groupby(preds["window_open"].dt.to_period("M")):
        calls = attach_market(simulate(g, p_col, tau, lead), candles)
        rows.append({
            "month": str(month),
            **summarize(calls, g["window_open"].nunique()),
            "brier_all_minutes": brier(g[p_col], g["label"]),
            "kalshi_labels": float((g["label_source"] == "kalshi").mean()) if "label_source" in g else 0.0,
            **market_metrics(calls),
        })
    return pd.DataFrame(rows)


def accuracy_by_minute(preds: pd.DataFrame, p_col: str) -> pd.DataFrame:
    """Accuracy if forced to call EVERY window at a fixed minute: the no-skipping benchmark."""
    g = preds.assign(hit=((preds[p_col] >= 0.5).astype(int) == preds["label"]).astype(int))
    return g.groupby("minute").agg(accuracy=("hit", "mean"), n=("hit", "size")).reset_index()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preds", default=str(WF_PATH))
    ap.add_argument("--tau", type=float, help="override the selected threshold")
    ap.add_argument("--lead", type=float, help="override the minimum lead time (seconds)")
    args = ap.parse_args()

    from .model import Predictor
    preds = pd.read_parquet(args.preds)
    pred = Predictor.load()
    tau = args.tau or pred.policy.tau
    lead = args.lead or pred.policy.min_lead_s
    candles = None
    try:
        con = db.connect(read_only=True)
        candles = db.load_kalshi_candles(con)
    except Exception:
        pass

    pd.set_option("display.width", 200, "display.max_columns", 30)
    for col in [c for c in ("p_bm", "p_gbm") if c in preds]:
        print(f"\n=== {col}  tau={tau}  min_lead={lead}s ===")
        print(monthly_table(preds, col, tau, lead, candles).round(4).to_string(index=False))
        print("\nforced call every window, by minute:")
        print(accuracy_by_minute(preds, col).round(4).T.to_string(header=False))
        print("\naccuracy vs coverage (all test months):")
        g = grid(preds, col, leads=[lead])
        print(g[["tau", "calls", "coverage", "accuracy", "acc_ci_low", "median_lock_minute"]].round(4).to_string(index=False))


if __name__ == "__main__":
    main()

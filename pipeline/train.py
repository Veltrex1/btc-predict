"""Build the dataset, run a walk-forward backtest, pick the lock policy, and save the model.

    uv run python -m pipeline.train                        # auto source, 3y history, 6 test months
    uv run python -m pipeline.train --source binance --years 8 --test-months 12

Walk-forward: for each test month M the model trains on data ending two months
before M, is calibrated on month M-1, and predicts M. Nothing from M (or later)
is ever seen. The lock threshold is chosen on all test months except the last;
the last month is an untouched holdout, reported separately: it is the
closest analogue of the graded live month.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from . import db
from .config import DATA_DIR, DEFAULT_SETTLE_MODE
from .evaluate import WF_PATH, monthly_table
from .features import FEATURES, decision_points, minute_frame, rows_from_points
from .labels import attach_kalshi, disagreement, proxy_windows
from .model import Policy, Predictor, fit_gbm, used_features
from .policy import select


def pick_source(con) -> str:
    counts = dict(con.execute("SELECT source, count(*) FROM btc_1m GROUP BY source").fetchall())
    if counts.get("coinbase", 0) > 100_000:
        return "coinbase"
    if counts.get("binance", 0) > 0:
        return "binance"
    raise SystemExit("no price data: run pipeline.backfill_coinbase or pipeline.backfill_binance first")


def build_dataset(bars: pd.DataFrame, markets: pd.DataFrame | None, settle_mode: str) -> tuple[pd.DataFrame, dict]:
    mf = minute_frame(bars)
    windows = attach_kalshi(proxy_windows(bars, settle_mode), markets)
    rows = rows_from_points(mf, decision_points(windows), settle_mode)
    rows = rows[np.isfinite(rows["sig_slow"]) & np.isfinite(rows["logret_target"]) & (rows["gap_frac"] <= 0.02)]
    rows[FEATURES] = rows[FEATURES].astype(np.float32)
    return rows.reset_index(drop=True), disagreement(windows)


def walk_forward(rows: pd.DataFrame, test_months: int = 6, train_years: float = 3.0, seed: int = 0) -> pd.DataFrame:
    period = rows["window_open"].dt.to_period("M")
    months = sorted(period.unique())
    if len(months) < 3:
        raise SystemExit("need at least 3 months of data for walk-forward")
    test = months[-test_months:] if len(months) > test_months + 2 else months[2:]
    out = []
    for M in test:
        m_start = M.start_time
        calib_mask = period == (M - 1)
        train_mask = (rows["window_open"] < (M - 1).start_time) & \
                     (rows["window_open"] >= m_start - pd.Timedelta(days=int(365 * train_years)))
        test_rows = rows[period == M].copy()
        clf, iso = fit_gbm(rows[train_mask], rows[calib_mask], seed)
        raw = clf.predict_proba(test_rows[used_features(clf)].to_numpy(dtype=np.float32))[:, 1]
        test_rows["p_gbm"] = iso.predict(raw) if iso is not None else raw
        print(f"  {M}: train {train_mask.sum():,} rows, test {len(test_rows):,} rows", flush=True)
        out.append(test_rows)
    return pd.concat(out, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["coinbase", "binance"])
    ap.add_argument("--years", type=float, default=3.0, help="history used for training (default 3)")
    ap.add_argument("--test-months", type=int, default=6)
    ap.add_argument("--settle", default=DEFAULT_SETTLE_MODE, choices=["avg60", "close"])
    ap.add_argument("--target-acc", type=float, default=0.78,
                    help="validation Wilson lower bound the threshold must clear (default 0.78, margin over 75%%)")
    ap.add_argument("--lead", type=float, default=90, help="minimum seconds left when a call locks")
    args = ap.parse_args()

    con = db.connect(read_only=True)
    source = args.source or pick_source(con)
    start = pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=int(365 * args.years) + 60)
    bars = db.load_bars(con, source, start=start)
    markets = db.load_kalshi_markets(con)
    print(f"source={source}: {len(bars):,} bars {bars.index.min()} → {bars.index.max()}; "
          f"kalshi markets: {len(markets):,}")

    rows, dis = build_dataset(bars, markets, args.settle)
    print(f"dataset: {len(rows):,} rows, {rows['window_open'].nunique():,} windows, "
          f"up-rate {rows.drop_duplicates('window_open')['label'].mean():.4f}")
    print("proxy vs Kalshi:", json.dumps(dis))

    preds = walk_forward(rows, args.test_months, args.years)
    WF_PATH.parent.mkdir(parents=True, exist_ok=True)
    preds.to_parquet(WF_PATH)

    last = preds["window_open"].dt.to_period("M").max()
    val = preds[preds["window_open"].dt.to_period("M") < last]
    hold = preds[preds["window_open"].dt.to_period("M") == last]
    choice = {}
    for col in ("p_bm", "p_gbm"):
        choice[col] = select(val, col, args.target_acc, args.lead)
        print(f"validation {col}: tau={choice[col]['tau']:.2f} coverage={choice[col]['coverage']:.3f} "
              f"accuracy={choice[col]['accuracy']:.4f} (CI low {choice[col]['acc_ci_low']:.4f})")
    best = max(choice, key=lambda c: (choice[c]["acc_ci_low"] >= args.target_acc, choice[c]["coverage"]))
    tau = float(choice[best]["tau"])

    candles = db.load_kalshi_candles(con)
    print(f"\nHOLDOUT month {last} with {best}, tau={tau:.2f}, lead={args.lead:.0f}s:")
    print(monthly_table(hold, best, tau, args.lead, candles).round(4).to_string(index=False))

    print("\nfitting final model ...")
    period = rows["window_open"].dt.to_period("M")
    final_month = period.max()
    clf, iso = fit_gbm(rows[period < final_month], rows[period == final_month])
    pred = Predictor(
        kind="gbm" if best == "p_gbm" else "baseline", settle_mode=args.settle,
        policy=Policy(tau=tau, min_lead_s=args.lead), clf=clf, iso=iso,
        meta={"source": source, "trained_through": str(rows["window_open"].max()),
              "validation": {k: {kk: float(vv) for kk, vv in v.items()} for k, v in choice.items()},
              "proxy_vs_kalshi": dis},
    )
    pred.save()
    print(f"saved model ({pred.kind}) with policy tau={tau:.2f}, lead={args.lead:.0f}s")


if __name__ == "__main__":
    main()

"""Reports for the class.

    uv run python -m pipeline.report --live 2026-11      # graded live month from data/live.sqlite
    uv run python -m pipeline.report --backtest          # walk-forward backtest from train.py

Writes reports/<name>.html (self-contained, charts embedded) and a CSV of every
window so the result can be audited window by window against Kalshi.
"""
from __future__ import annotations

import argparse
import base64
import io
import sqlite3

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .config import LIVE_DB_PATH, REPORT_DIR  # noqa: E402
from .evaluate import WF_PATH, accuracy_by_minute, brier, wilson  # noqa: E402
from .kalshi import fee_per_contract  # noqa: E402
from .policy import grid  # noqa: E402

SERIES = {"p_bm": ("#2a78d6", "Brownian baseline"), "p_gbm": ("#eb6834", "Gradient-boosted model")}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e7e6e2"


def _style(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", color=INK, fontsize=12)
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED)


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="#fcfcfb")
    plt.close(fig)
    return f'<img alt="" src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'


def calibration_chart(df: pd.DataFrame, cols: dict[str, str], title: str) -> str:
    """cols: {prob column: label column}."""
    fig, ax = plt.subplots(figsize=(5.2, 4.6))
    ax.plot([0, 1], [0, 1], color=MUTED, linewidth=1, linestyle="--", label="perfect calibration")
    for col, ycol in cols.items():
        color, name = SERIES.get(col, ("#2a78d6", col))
        b = pd.cut(df[col], np.linspace(0, 1, 11), include_lowest=True)
        g = df.groupby(b, observed=True).agg(p=(col, "mean"), y=(ycol, "mean"), n=(ycol, "size"))
        g = g[g["n"] >= 20]
        ax.plot(g["p"], g["y"], color=color, linewidth=2, marker="o", markersize=5, label=name)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    _style(ax, title, "predicted P(above)", "observed share above")
    ax.legend(frameon=False, labelcolor=INK)
    return _png(fig)


def backtest_report(path=WF_PATH, lead: float = 90) -> str:
    preds = pd.read_parquet(path)
    cols = [c for c in SERIES if c in preds]
    parts = [f"<h1>Walk-forward backtest</h1><p>{preds['window_open'].min():%Y-%m-%d} → "
             f"{preds['window_open'].max():%Y-%m-%d}, {preds['window_open'].nunique():,} windows, "
             f"minimum lead {lead:.0f}s. Every prediction is out-of-sample.</p>"]

    fig, ax = plt.subplots(figsize=(7, 4.2))
    for c in cols:
        g = grid(preds, c, leads=[lead])
        color, name = SERIES[c]
        ax.plot(g["coverage"], g["accuracy"], color=color, linewidth=2, marker="o", markersize=4, label=name)
    ax.axhline(0.75, color=MUTED, linewidth=1, linestyle="--")
    ax.text(0.01, 0.755, "75% target", color=MUTED, fontsize=9, transform=ax.get_yaxis_transform())
    _style(ax, "Accuracy vs. share of windows called", "coverage (share of windows with a locked call)",
           "accuracy on calls")
    ax.legend(frameon=False, labelcolor=INK)
    parts.append(_png(fig))

    fig, ax = plt.subplots(figsize=(7, 4.2))
    for c in cols:
        a = accuracy_by_minute(preds, c)
        color, name = SERIES[c]
        ax.plot(a["minute"], a["accuracy"], color=color, linewidth=2, marker="o", markersize=4, label=name)
    ax.axhline(0.75, color=MUTED, linewidth=1, linestyle="--")
    _style(ax, "If forced to call every window at a fixed minute", "minute of window", "accuracy")
    ax.legend(frameon=False, labelcolor=INK)
    parts.append(_png(fig))
    parts.append(calibration_chart(preds, {c: "label" for c in cols}, "Calibration (all minutes)"))
    for c in cols:
        g = grid(preds, c, leads=[lead])
        parts.append(f"<h2>{SERIES[c][1]}: threshold table</h2>" +
                     g[["tau", "calls", "coverage", "accuracy", "acc_ci_low", "acc_ci_high",
                        "median_lock_minute"]].round(4).to_html(index=False))
        parts.append(f"<p>Brier score (all minutes): {brier(preds[c], preds['label']):.4f}</p>")
    return "\n".join(parts)


def live_frame(month: str, db_path=LIVE_DB_PATH) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    df = pd.read_sql("SELECT * FROM windows WHERE substr(window_open, 1, 7) = ? ORDER BY window_open",
                     con, params=(month,))
    df["eligible"] = df["first_seen_secs_left"].fillna(0) >= df["min_lead_s"].fillna(90)
    df["called"] = df["locked_side"].notna()
    df["settled"] = df["result"].isin(["yes", "no"])
    df["correct"] = np.where(df["called"] & df["settled"],
                             ((df["locked_side"] == "ABOVE") == (df["result"] == "yes")).astype(float), np.nan)
    ask = np.where(df["locked_side"] == "ABOVE", df["yes_ask_at_lock"], df["no_ask_at_lock"])
    df["cost_at_lock"] = ask
    df["pnl_1_contract"] = [
        (c - a - fee_per_contract(a)) if pd.notna(c) and pd.notna(a) else np.nan
        for c, a in zip(df["correct"], ask)
    ]
    return df


def live_report(month: str) -> tuple[str, pd.DataFrame]:
    df = live_frame(month)
    e = df[df["eligible"]]
    calls = e[e["called"] & e["settled"]]
    k, n = int(calls["correct"].sum()), len(calls)
    lo, hi = wilson(k, n)
    acc = k / n if n else float("nan")
    cover = e["called"].mean() if len(e) else float("nan")
    pnl = calls["pnl_1_contract"].sum()
    parts = [f"<h1>Live month {month}</h1>",
             "<table><tr><th>windows observed</th><th>calls</th><th>settled calls</th><th>correct</th>"
             "<th>accuracy</th><th>95% CI</th><th>coverage</th><th>P&amp;L, 1 contract at lock ask</th></tr>"
             f"<tr><td>{len(e)}</td><td>{int(e['called'].sum())}</td><td>{n}</td><td>{k}</td>"
             f"<td><b>{acc:.1%}</b></td><td>{lo:.1%} – {hi:.1%}</td><td>{cover:.1%}</td>"
             f"<td>${pnl:+.2f}</td></tr></table>",
             "<p>Ground truth is Kalshi's official result for each KXBTC15M market. A window counts as "
             "observed if the server was running early enough to make a call; a call locks once and never "
             "changes. Windows without a call are NO CALL and excluded from accuracy (reported as coverage).</p>"]
    if n:
        daily = calls.assign(day=calls["window_open"].str[:10]).groupby("day").agg(
            calls=("correct", "size"), accuracy=("correct", "mean"), pnl=("pnl_1_contract", "sum"))
        parts.append("<h2>By day</h2>" + daily.round(3).to_html())
        side = calls.assign(p=np.where(calls["locked_side"] == "ABOVE", calls["locked_prob"], 1 - calls["locked_prob"]))
        parts.append(calibration_chart(side.assign(p_bm=side["p"]), {"p_bm": "correct"},
                                       "Confidence at lock vs. hit rate"))
    return "\n".join(parts), df


def write_html(name: str, body: str) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{name}.html"
    path.write_text(
        "<!doctype html><meta charset='utf-8'><title>btc-predict report</title>"
        "<style>body{font:14px/1.5 -apple-system,Segoe UI,sans-serif;max-width:900px;margin:32px auto;"
        "padding:0 16px;color:#0b0b0b;background:#fcfcfb}img{max-width:100%;display:block;margin:16px 0}"
        "table{border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{border-bottom:1px solid #e7e6e2;"
        "padding:4px 10px;text-align:right}</style>" + body)
    return str(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", metavar="YYYY-MM")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--lead", type=float, default=90)
    args = ap.parse_args()
    if args.backtest:
        print(write_html("backtest", backtest_report(lead=args.lead)))
    if args.live:
        body, df = live_report(args.live)
        print(write_html(f"live-{args.live}", body))
        csv = REPORT_DIR / f"live-{args.live}.csv"
        df.to_csv(csv, index=False)
        print(csv)
    if not (args.backtest or args.live):
        ap.print_help()


if __name__ == "__main__":
    main()

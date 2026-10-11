"""Lock policy: at most one call per window, made the first moment the model is
confident enough and never changed afterwards. Windows where that never happens
(before the minimum lead time) are NO CALL.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TAUS = np.round(np.arange(0.60, 0.981, 0.02), 3)
LEADS = [60, 90, 120, 180, 300, 420, 600]


def simulate(preds: pd.DataFrame, p_col: str, tau: float, min_lead_s: float) -> pd.DataFrame:
    """One row per called window: side, prob at lock, minute, secs_left, correct."""
    p = preds[p_col].to_numpy()
    ok = (preds["secs_left"].to_numpy() >= min_lead_s) & ((p >= tau) | (p <= 1 - tau))
    cand = preds.loc[ok]
    if cand.empty:
        return cand.assign(side=pd.Series(dtype=str), prob=pd.Series(dtype=float), correct=pd.Series(dtype=int))
    first = cand.sort_values(["window_open", "secs_left"], ascending=[True, False]).drop_duplicates("window_open")
    first = first.copy()
    first["prob"] = first[p_col]
    first["side"] = np.where(first["prob"] >= 0.5, "ABOVE", "BELOW")
    first["correct"] = ((first["side"] == "ABOVE").astype(int) == first["label"]).astype(int)
    return first


def summarize(calls: pd.DataFrame, n_windows: int) -> dict:
    from .evaluate import wilson
    n = len(calls)
    k = int(calls["correct"].sum()) if n else 0
    lo, hi = wilson(k, n)
    return {
        "windows": int(n_windows),
        "calls": n,
        "coverage": n / n_windows if n_windows else 0.0,
        "accuracy": k / n if n else float("nan"),
        "acc_ci_low": lo,
        "acc_ci_high": hi,
        "median_lock_minute": float(15 - calls["secs_left"].median() / 60) if n else float("nan"),
    }


def grid(preds: pd.DataFrame, p_col: str, taus=TAUS, leads=LEADS) -> pd.DataFrame:
    n_windows = preds["window_open"].nunique()
    rows = []
    for lead in leads:
        for tau in taus:
            s = summarize(simulate(preds, p_col, tau, lead), n_windows)
            rows.append({"tau": float(tau), "min_lead_s": lead, **s})
    return pd.DataFrame(rows)


def select(preds: pd.DataFrame, p_col: str, target_acc: float = 0.80, min_lead_s: float = 90,
           min_calls: int = 200) -> dict:
    """Highest-coverage tau whose Wilson LOWER bound on validation accuracy clears target_acc."""
    g = grid(preds, p_col, leads=[min_lead_s])
    ok = g[(g["acc_ci_low"] >= target_acc) & (g["calls"] >= min_calls)]
    if ok.empty:
        best = g.sort_values("accuracy", ascending=False).iloc[0]
    else:
        best = ok.sort_values("coverage", ascending=False).iloc[0]
    return best.to_dict()

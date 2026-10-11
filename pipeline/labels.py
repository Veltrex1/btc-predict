"""Window targets and outcomes.

A Kalshi 15-minute window opens at :00/:15/:30/:45 UTC. YES ("above") wins if
the settlement price at close is above the target set at open. Kalshi's own
target and result are used whenever we have them; otherwise both are
approximated from 1-minute bars (see config.DEFAULT_SETTLE_MODE).
"""
from __future__ import annotations

import pandas as pd

from .config import WINDOW_MIN

MIN = pd.Timedelta(minutes=1)
WIN = pd.Timedelta(minutes=WINDOW_MIN)


def reference_price(bars: pd.DataFrame, mode: str) -> pd.Series:
    """Per-bar settlement reference, indexed by bar start."""
    if mode == "avg60":
        return (bars["open"] + bars["high"] + bars["low"] + bars["close"]) / 4.0
    if mode == "close":
        return bars["close"]
    raise ValueError(f"unknown settle mode {mode!r}")


def proxy_windows(bars: pd.DataFrame, mode: str) -> pd.DataFrame:
    """One row per window: proxy target (last minute before open) and proxy settle (last minute of window)."""
    ref = reference_price(bars, mode)
    first = (bars.index.min() + MIN).ceil(f"{WINDOW_MIN}min")
    last = (bars.index.max() + MIN).floor(f"{WINDOW_MIN}min") - WIN
    opens = pd.date_range(first, last, freq=f"{WINDOW_MIN}min")
    w = pd.DataFrame(index=opens)
    w.index.name = "window_open"
    w["target_proxy"] = ref.reindex(opens - MIN).to_numpy()
    w["settle_proxy"] = ref.reindex(opens + WIN - MIN).to_numpy()
    w = w.dropna()
    w["label_proxy"] = (w["settle_proxy"] > w["target_proxy"]).astype(int)
    return w


def attach_kalshi(windows: pd.DataFrame, markets: pd.DataFrame | None) -> pd.DataFrame:
    """Prefer Kalshi's target/result per window; fall back to the proxies."""
    w = windows.copy()
    w["ticker"] = None
    w["target"] = w["target_proxy"]
    w["label"] = w["label_proxy"]
    w["label_source"] = "proxy"
    if markets is None or markets.empty:
        return w
    m = markets.dropna(subset=["close_time"]).copy()
    m["window_open"] = pd.to_datetime(m["close_time"]) - WIN
    m = m.drop_duplicates("window_open", keep="last").set_index("window_open")
    m = m.reindex(w.index)
    has_target = m["target"].notna()
    has_result = m["result"].isin(["yes", "no"])
    w.loc[m["ticker"].notna(), "ticker"] = m["ticker"]
    w.loc[has_target, "target"] = m.loc[has_target, "target"]
    w.loc[has_result, "label"] = (m.loc[has_result, "result"] == "yes").astype(int)
    w.loc[has_result, "label_source"] = "kalshi"
    return w


def disagreement(windows: pd.DataFrame) -> dict:
    """How often the price proxy disagrees with Kalshi's official result."""
    k = windows[windows["label_source"] == "kalshi"]
    if k.empty:
        return {"n": 0}
    proxy_vs_kalshi_target = (k["settle_proxy"] > k["target"]).astype(int)
    return {
        "n": int(len(k)),
        "proxy_label_disagree": float((k["label_proxy"] != k["label"]).mean()),
        "proxy_settle_vs_kalshi_target_disagree": float((proxy_vs_kalshi_target != k["label"]).mean()),
        "median_abs_target_gap_usd": float((k["target"] - k["target_proxy"]).abs().median()),
    }

"""Features for "will BTC close this window above its target?".

Every feature at decision time t uses only bars that have ended by t. The same
code path serves training (one row per window per minute) and the live server
(one row for "now", with a partial current bar).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import ndtr

from .config import WINDOW_MIN

MIN = pd.Timedelta(minutes=1)
WINDOW_SECS = WINDOW_MIN * 60

FEATURES = [
    "secs_left", "z", "p_bm", "logret_target", "sig_fast", "sig_slow", "vol_ratio",
    "ret_1", "ret_5", "ret_15", "ret_60", "range_pos", "taker_imb_5",
    "hour_sin", "hour_cos", "dow",
]


def minute_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """Rolling per-minute state, indexed by bar END (start + 1 minute)."""
    bars = bars.sort_index()
    grid = pd.date_range(bars.index.min(), bars.index.max(), freq="1min")
    b = bars.reindex(grid)
    close = b["close"].ffill()
    high = b["high"].fillna(close)
    low = b["low"].fillna(close)
    vol = b["volume"].fillna(0.0)
    taker = b["taker_buy"] if "taker_buy" in b else pd.Series(np.nan, index=grid)

    lc = np.log(close)
    lr = lc.diff().fillna(0.0)
    mf = pd.DataFrame(index=grid + MIN)
    mf.index.name = "end"
    mf["close"] = close.to_numpy()
    mf["high"] = high.to_numpy()
    mf["low"] = low.to_numpy()
    mf["sig_fast"] = lr.ewm(halflife=30, min_periods=30).std().to_numpy()
    mf["sig_slow"] = lr.rolling(1440, min_periods=240).std().to_numpy()
    for k in (1, 5, 15, 60):
        mf[f"ret_{k}"] = (lc - lc.shift(k)).to_numpy()
    tv = taker.rolling(5, min_periods=1).sum()
    vv = vol.rolling(5, min_periods=1).sum()
    mf["taker_imb_5"] = (2 * tv / vv.where(vv > 0) - 1).to_numpy()
    # share of the trailing day with no real bar (data holes); rows near holes are dropped in training
    mf["gap_frac"] = b["close"].isna().rolling(1440, min_periods=1).mean().to_numpy()
    win = (mf.index - MIN).floor(f"{WINDOW_MIN}min")
    mf["win"] = win
    mf["hi_w"] = mf.groupby("win")["high"].cummax()
    mf["lo_w"] = mf.groupby("win")["low"].cummin()
    return mf


def decision_points(windows: pd.DataFrame, minutes=range(WINDOW_MIN)) -> pd.DataFrame:
    """Window x minute grid. `at` is the decision time (a bar end)."""
    parts = []
    for m in minutes:
        p = pd.DataFrame({
            "window_open": windows.index,
            "minute": m,
            "at": windows.index + pd.Timedelta(minutes=m),
            "secs_left": float(WINDOW_SECS - 60 * m),
            "target": windows["target"].to_numpy(),
        })
        for col in ("label", "label_source", "ticker"):
            if col in windows:
                p[col] = windows[col].to_numpy()
        parts.append(p)
    return pd.concat(parts, ignore_index=True)


def effective_minutes(secs_left, settle_mode: str):
    """Remaining variance in minute units. Averaging the final 60s removes ~2/3 of a minute of variance."""
    mins = np.asarray(secs_left, dtype=float) / 60.0
    if settle_mode == "avg60":
        mins = mins - 2.0 / 3.0
    return np.maximum(mins, 1.0 / 6.0)


def rows_from_points(mf: pd.DataFrame, pts: pd.DataFrame, settle_mode: str = "avg60",
                     price=None) -> pd.DataFrame:
    """Attach features to decision points. `price` overrides the bar close (live use)."""
    j = mf.reindex(pd.DatetimeIndex(pts["at"]))
    out = pts.reset_index(drop=True).copy()
    P = np.asarray(price if price is not None else j["close"], dtype=float)
    K = out["target"].to_numpy(dtype=float)
    sig_fast = j["sig_fast"].to_numpy()
    sig_slow = j["sig_slow"].to_numpy()
    sig = np.where(np.isfinite(sig_fast), sig_fast, sig_slow)
    sig = np.clip(np.nan_to_num(sig, nan=1e-3), 1e-5, None)

    out["logret_target"] = np.log(P / K)
    out["z"] = out["logret_target"] / (sig * np.sqrt(effective_minutes(out["secs_left"], settle_mode)))
    out["p_bm"] = ndtr(out["z"])
    out["sig_fast"] = sig_fast
    out["sig_slow"] = sig_slow
    out["vol_ratio"] = sig_fast / np.where(sig_slow > 0, sig_slow, np.nan)
    for k in (1, 5, 15, 60):
        out[f"ret_{k}"] = j[f"ret_{k}"].to_numpy()
    in_window = out["secs_left"].to_numpy() < WINDOW_SECS
    hi = np.maximum(j["hi_w"].to_numpy(), P)
    lo = np.minimum(j["lo_w"].to_numpy(), P)
    rng = hi - lo
    pos = np.where(rng > 0, (P - lo) / np.where(rng > 0, rng, 1.0), 0.5)
    out["range_pos"] = np.where(in_window, pos, 0.5)
    out["taker_imb_5"] = j["taker_imb_5"].to_numpy()
    at = pd.DatetimeIndex(out["at"])
    hour = at.hour + at.minute / 60.0
    out["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    out["dow"] = at.dayofweek.astype(float)
    out["price"] = P
    out["gap_frac"] = j["gap_frac"].to_numpy()
    return out


def live_row(bars: pd.DataFrame, now: pd.Timestamp, price: float, window_open: pd.Timestamp,
             target: float, settle_mode: str = "avg60") -> pd.DataFrame:
    """Feature row for the current moment. `bars` are completed 1m bars (index = bar start)."""
    cur_start = now.floor("1min")
    hist = bars[bars.index < cur_start]
    partial = hist.iloc[-1:].copy() if len(hist) else pd.DataFrame(columns=bars.columns)
    partial.index = [cur_start]
    partial[["open", "high", "low", "close"]] = price
    partial["volume"] = 0.0
    partial["taker_buy"] = np.nan
    mf = minute_frame(pd.concat([hist.tail(1600), partial]))
    secs_left = max(0.0, (window_open + pd.Timedelta(minutes=WINDOW_MIN) - now).total_seconds())
    pts = pd.DataFrame({
        "window_open": [window_open],
        "minute": [int((now - window_open).total_seconds() // 60)],
        "at": [cur_start + MIN],
        "secs_left": [secs_left],
        "target": [target],
    })
    return rows_from_points(mf, pts, settle_mode, price=[price])

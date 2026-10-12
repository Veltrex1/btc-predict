import numpy as np
import pandas as pd

from pipeline.evaluate import market_metrics, wilson
from pipeline.features import FEATURES, decision_points, live_row, minute_frame, rows_from_points
from pipeline.kalshi import dollars, fee_per_contract, market_target, parse_candles, parse_market
from pipeline.labels import attach_kalshi, proxy_windows
from pipeline.policy import simulate

from .conftest import synthetic_bars


def _rows(bars, mode="avg60"):
    w = attach_kalshi(proxy_windows(bars, mode), None)
    return rows_from_points(minute_frame(bars), decision_points(w), mode)


def test_proxy_windows_align_to_quarter_hours(bars):
    w = proxy_windows(bars, "close")
    assert (w.index.minute % 15 == 0).all()
    W = w.index[10]
    assert w.loc[W, "target_proxy"] == bars.loc[W - pd.Timedelta(minutes=1), "close"]
    assert w.loc[W, "settle_proxy"] == bars.loc[W + pd.Timedelta(minutes=14), "close"]
    # Kalshi rule is "at least": a tie settles YES
    assert (w["label_proxy"] == (w["settle_proxy"] >= w["target_proxy"]).astype(int)).all()


def test_features_do_not_look_ahead(bars):
    """Changing every bar after time t must not change any feature row decided at or before t."""
    cut = bars.index[len(bars) // 2].floor("15min")
    w = attach_kalshi(proxy_windows(bars, "close"), None)
    pts = decision_points(w)
    pts = pts[pts["at"] <= cut]
    a = rows_from_points(minute_frame(bars), pts, "close")
    tampered = bars.copy()
    after = tampered.index >= cut
    tampered.loc[after, ["open", "high", "low", "close"]] *= 1.5
    tampered.loc[after, "taker_buy"] = 0
    b = rows_from_points(minute_frame(tampered), pts, "close")
    pd.testing.assert_frame_equal(a[FEATURES], b[FEATURES])


def test_live_row_matches_offline_row(bars):
    rows = _rows(bars)
    r = rows[(rows["minute"] == 7) & rows["sig_slow"].notna()].iloc[100]
    now = r["at"]
    # live: the bar ending at `now` is complete; feed the next bar's start with the same price
    live = live_row(bars, now, float(r["price"]), r["window_open"], float(r["target"]))
    assert abs(live["secs_left"].iloc[0] - r["secs_left"]) < 1e-9
    assert abs(live["z"].iloc[0] - r["z"]) / max(abs(r["z"]), 1e-6) < 0.05


def test_baseline_is_calibrated_on_random_walk():
    rows = _rows(synthetic_bars(days=40, seed=3)).dropna(subset=["sig_slow"])
    rows["bucket"] = pd.cut(rows["p_bm"], [0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0])
    cal = rows.groupby("bucket", observed=True).agg(p=("p_bm", "mean"), y=("label", "mean"), n=("label", "size"))
    cal = cal[cal["n"] > 500]
    assert (cal["p"] - cal["y"]).abs().max() < 0.06, cal


def test_policy_locks_once_and_respects_lead():
    preds = pd.DataFrame({
        "window_open": [pd.Timestamp("2026-01-01")] * 4,
        "secs_left": [900.0, 600.0, 300.0, 60.0],
        "p": [0.5, 0.9, 0.1, 0.99],
        "label": [1] * 4,
    })
    calls = simulate(preds, "p", 0.8, 90)
    assert len(calls) == 1
    assert calls.iloc[0]["side"] == "ABOVE" and calls.iloc[0]["secs_left"] == 600
    assert simulate(preds.iloc[[0, 3]], "p", 0.8, 90).empty  # only confident at 60s left -> no call


def test_wilson():
    lo, hi = wilson(80, 100)
    assert 0.70 < lo < 0.72 and 0.86 < hi < 0.88


def test_fee():
    assert fee_per_contract(0.5) == 0.0175
    assert fee_per_contract(0.9) == 0.0063
    assert fee_per_contract(0.99) == 0.0007  # 0.000693 rounds up to $0.0001


def test_market_metrics_cost_and_pnl():
    calls = pd.DataFrame({"side": ["ABOVE", "BELOW"], "correct": [1, 0], "label": [1, 1],
                          "yes_bid": [0.80, 0.70], "yes_ask": [0.82, 0.72]})
    m = market_metrics(calls)
    # ABOVE pays 0.82 + fee, wins 1; BELOW pays 1-0.70=0.30 + fee, loses
    expected = (1 - 0.82 - fee_per_contract(0.82)) + (0 - 0.30 - fee_per_contract(0.30))
    assert abs(m["total_pnl_1_contract"] - expected) < 1e-9


def test_kalshi_parsing_cents_and_dollars():
    assert dollars({"yes_bid": 55}, "yes_bid") == 0.55
    assert dollars({"yes_bid_dollars": "0.5600", "yes_bid": 55}, "yes_bid") == 0.56
    m = {"ticker": "KXBTC15M-26OCT111830-30", "open_time": "2026-10-11T22:15:00Z",
         "close_time": "2026-10-11T22:30:00Z", "floor_strike": 83747.32, "result": "yes",
         "expiration_value": "83801.12", "status": "finalized"}
    p = parse_market(m)
    assert p["target"] == 83747.32 and p["result"] == "yes" and p["close_time"] == pd.Timestamp("2026-10-11 22:30")
    assert market_target({"yes_sub_title": "Target Price: $83,747.32"}) == 83747.32
    c = parse_candles("T", [{"end_period_ts": 1791750960, "yes_bid": {"close": 61}, "yes_ask": {"close_dollars": "0.6300"},
                             "price": {"close": None}, "volume": 12}])
    assert c.iloc[0]["yes_bid"] == 0.61 and c.iloc[0]["yes_ask"] == 0.63


def test_attach_kalshi_prefers_official(bars):
    w = proxy_windows(bars, "avg60")
    W = w.index[5]
    markets = pd.DataFrame([{"ticker": "X", "open_time": W, "close_time": W + pd.Timedelta(minutes=15),
                             "target": 1.0, "result": "no", "expiration_value": 2.0, "status": "settled"}])
    a = attach_kalshi(w, markets)
    assert a.loc[W, "target"] == 1.0 and a.loc[W, "label"] == 0 and a.loc[W, "label_source"] == "kalshi"
    assert (a.drop(index=W)["label_source"] == "proxy").all()
    assert np.isfinite(a["target"]).all()

import pandas as pd

from pipeline import arb
from pipeline.strategy import fee


def test_arb_edge_and_slug_join():
    close = int(pd.Timestamp("2026-10-12 05:00", tz="UTC").timestamp() * 1000)
    mk = pd.DataFrame([{"ticker": "K1", "close_ts": close, "target": 100.0, "result": "yes", "expiration_value": 101.0}])
    sec_ms = close - 300_000
    kb = pd.DataFrame([{"ts": sec_ms, "ticker": "K1", "yes_bid": 0.47, "yes_bid_sz": 50, "yes_ask": 0.48, "yes_ask_sz": 40}])
    pm = pd.DataFrame([{"ts": sec_ms + 200, "slug": "cpc-btc-updown-15m-2026-10-12-0445z",
                        "best_bid": 0.55, "bid_sz": 30, "best_ask": 0.56, "ask_sz": 20}])
    d = arb.panel(pm, kb, mk)
    assert len(d) == 1
    # A: Kalshi YES 0.48 + PM-US Down at 1 - 0.55 = 0.45 -> 0.93 before fees
    r = d.iloc[0]
    assert abs(r.cost_a - 0.93) < 1e-9
    assert abs(r.edge_a - (1 - 0.93 - fee(1, 0.48) - arb.pmus_fee(1, 0.55))) < 1e-9
    assert r.size_a == 30
    t = arb.first_trades(d)
    assert len(t) == 1 and t.iloc[0].edge > 0.03


def test_arb_no_data():
    assert arb.summary(pd.DataFrame())["status"].startswith("no overlapping")


def _two_seconds(pm_up, second_bid=0.55):
    close = int(pd.Timestamp("2026-10-12 05:00", tz="UTC").timestamp() * 1000)
    mk = pd.DataFrame([{"ticker": "K1", "close_ts": close, "target": 100.0, "result": "yes", "expiration_value": 101.0}])
    t = close - 300_000
    kb = pd.DataFrame([{"ts": t + i * 1000, "ticker": "K1", "yes_bid": 0.47, "yes_bid_sz": 50, "yes_ask": 0.48,
                        "yes_ask_sz": 40} for i in range(2)])
    slug = "cpc-btc-updown-15m-2026-10-12-0445z"
    pm = pd.DataFrame([{"ts": t + 200, "slug": slug, "best_bid": 0.55, "bid_sz": 30, "best_ask": 0.56, "ask_sz": 20},
                       {"ts": t + 1200, "slug": slug, "best_bid": second_bid, "bid_sz": 30, "best_ask": 0.56,
                        "ask_sz": 20}])
    res = pd.DataFrame([{"slug": slug, "settle_px": pm_up}])
    return arb.panel(pm, kb, mk, res)


def test_realised_pnl_equals_edge_when_venues_agree():
    t = arb.first_trades(_two_seconds(pm_up=1.0))
    assert t.iloc[0].side == "A" and t.iloc[0].payout == 1
    assert abs(t.iloc[0].pnl - t.iloc[0].edge) < 1e-9


def test_split_settlement_loses_the_whole_cost():
    d = _two_seconds(pm_up=1.0)
    d["result"] = "no"  # Kalshi NO, Polymarket Up: leg A (Kalshi YES + PM Down) pays nothing
    t = arb.first_trades(d)
    assert t.iloc[0].payout == 0
    assert abs(t.iloc[0].pnl - (t.iloc[0].edge - 1)) < 1e-9
    assert arb.splits(d)["windows_settled_differently"] == 1


def test_gap_that_vanishes_next_second_is_not_traded():
    d = _two_seconds(pm_up=1.0, second_bid=0.50)  # second 2: 0.48 + 0.50 -> no gap left
    assert len(arb.first_trades(d)) == 1
    assert arb.first_trades(d, delay=1).empty
    assert arb.persistence(d) == 0.0
    assert arb.persistence(_two_seconds(pm_up=1.0)) == 1.0


def test_unknown_polymarket_result_leaves_pnl_blank():
    d = _two_seconds(pm_up=float("nan"))
    assert arb.first_trades(d).pnl.isna().all()

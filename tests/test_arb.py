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

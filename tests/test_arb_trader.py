import time

import pandas as pd
import pytest

from pipeline import arb
from server import arb_trader as at
from server.kalshi_client import Book, Fill

KB = Book(yes_bid=0.47, yes_bid_size=50, yes_ask=0.48, yes_ask_size=40)
PB = at.PMBook(best_bid=0.55, bid_sz=30, best_ask=0.56, ask_sz=20)


def test_opportunity_matches_research_panel():
    o = at.opportunity(KB, PB, 300, at.Config(min_edge=0.005))
    close = int(pd.Timestamp("2026-10-12 05:00", tz="UTC").timestamp() * 1000)
    mk = pd.DataFrame([{"ticker": "K", "close_ts": close, "target": 1.0, "result": "yes", "expiration_value": 1.0}])
    kb = pd.DataFrame([{"ts": close - 300_000, "ticker": "K", "yes_bid": 0.47, "yes_bid_sz": 50, "yes_ask": 0.48,
                        "yes_ask_sz": 40}])
    pm = pd.DataFrame([{"ts": close - 300_000, "slug": "cpc-btc-updown-15m-2026-10-12-0445z", "best_bid": 0.55,
                        "bid_sz": 30, "best_ask": 0.56, "ask_sz": 20}])
    d = arb.panel(pm, kb, mk).iloc[0]
    assert o["side"] == "A" and abs(o["edge"] - d.edge) < 1e-9
    assert o["pm_intent"] == "ORDER_INTENT_BUY_SHORT" and o["pm_yes"] == 0.55 and o["pm_price"] == 0.45
    assert o["qty"] == 10  # min(max 10, half of the 30 at the touch)


def test_no_opportunity_near_close_or_below_edge():
    assert at.opportunity(KB, PB, 30, at.Config()) is None
    assert at.opportunity(KB, at.PMBook(0.50, 30, 0.53, 20), 300, at.Config()) is None


def row(**kw):
    base = {"k_side": "yes", "pm_intent": "ORDER_INTENT_BUY_SHORT", "pm_filled": 10, "pm_avg": 0.45, "pm_fee": 0.17,
            "k_filled": 10, "k_avg": 0.48, "k_fee": 0.18, "unwind_filled": 0, "unwind_avg": None, "unwind_fee": 0,
            "k_result": "yes", "pm_up": 1.0}
    return {**base, **kw}


def test_pair_pnl_hedged_and_split():
    payout, pnl = at.pair_pnl(row())
    assert payout == 10 and abs(pnl - (10 - 4.5 - 0.17 - 4.8 - 0.18)) < 1e-9
    payout, pnl = at.pair_pnl(row(k_result="no"))  # Kalshi NO, Polymarket Up: both legs lose
    assert payout == 0 and pnl < -9
    assert at.pair_pnl(row(pm_up=None)) is None


def test_pair_pnl_after_unwind_needs_no_polymarket_result():
    r = row(k_filled=0, k_avg=None, k_fee=0, unwind_filled=10, unwind_avg=0.43, unwind_fee=0.17, pm_up=None)
    payout, pnl = at.pair_pnl(r)
    assert payout == 0 and abs(pnl - (-4.5 - 0.17 + 4.3 - 0.17)) < 1e-9


class FakePM:
    def __init__(self, fill):
        self.fill, self.orders = fill, []

    def place(self, slug, intent, yes_price, qty, preview=False):
        self.orders.append((intent, yes_price, qty))
        q = self.fill if not self.orders[1:] else qty
        return {"order_id": f"o{len(self.orders)}", "filled": q, "avg_yes_price": yes_price, "fee": None}

    def balance(self):
        return {"cash": 20.0, "buying_power": 20.0}


class FakeKalshi:
    def __init__(self, fills):
        self.fills, self.orders = list(fills), []

    def place(self, ticker, kind, side, count, price):
        self.orders.append((kind, side, count, price))
        q = min(count, self.fills.pop(0) if self.fills else 0)
        return Fill("k", q, price if q else None, 0.0175 if q else None)

    def book(self, ticker):
        return KB

    def balance(self):
        return {"cash": 20.0}


@pytest.fixture
def trader(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "STOP_FILE", tmp_path / "STOP")

    def make(mode="live", kalshi=None, pm=None):
        t = at.ArbTrader(at.Config(mode=mode), kalshi, pm, tmp_path / "arb.sqlite")
        t.market = {"ticker": "KX-1", "slug": "cpc-x", "close_ts": int(time.time() * 1000) + 600_000}
        t.pmbook = lambda slug: PB
        t.kbook = lambda ticker: KB
        t.settle = lambda force=False: None
        return t
    return make


def test_live_pair_both_legs_fill(trader):
    pm, k = FakePM(fill=10), FakeKalshi([10])
    t = trader(kalshi=k, pm=pm)
    r = t.step()
    assert r["status"] == "hedged" and r["pm_filled"] == 10 and r["k_filled"] == 10
    assert pm.orders == [("ORDER_INTENT_BUY_SHORT", 0.55, 10)]
    assert k.orders[0][:3] == ("buy", "yes", 10) and k.orders[0][3] >= 0.48
    assert t.step() is None  # one pair per window


def test_live_second_leg_misses_then_unwinds(trader):
    pm, k = FakePM(fill=10), FakeKalshi([4, 0])
    t = trader(kalshi=k, pm=pm)
    r = t.step()
    assert r["k_filled"] == 4 and len(k.orders) == 2
    assert pm.orders[1][0] == "ORDER_INTENT_SELL_SHORT" and pm.orders[1][2] == 6
    assert r["unwind_filled"] == 6 and r["status"] == "unwound" and t.halted is None


def test_unhedged_leftover_halts(trader):
    class NoUnwind(FakePM):
        def place(self, slug, intent, yes_price, qty, preview=False):
            if intent.startswith("ORDER_INTENT_SELL"):
                raise RuntimeError("rejected")
            return super().place(slug, intent, yes_price, qty)
    t = trader(kalshi=FakeKalshi([0, 0]), pm=NoUnwind(fill=10))
    assert t.step()["status"] == "unhedged" and t.halted.startswith("unhedged")
    assert t.step() is None


def test_stop_file_and_daily_stop(trader, tmp_path):
    t = trader(kalshi=FakeKalshi([10]), pm=FakePM(fill=10))
    (tmp_path / "STOP").touch()
    assert t.step() is None and t.halted == "stop file"
    t2 = trader(mode="shadow")
    t2.db.execute("DELETE FROM arb_state")
    t2.db.execute("INSERT INTO attempts (mode, pnl, settled_ts, status) VALUES ('shadow', -6, ?, 'hedged')",
                  (pd.Timestamp.now("UTC").isoformat(),))
    (tmp_path / "STOP").unlink()
    assert t2.step() is None and t2.halted.startswith("daily stop")


def test_shadow_fills_from_fresh_books(trader, tmp_path):
    t = trader(mode="shadow")
    r = t.step()
    assert r["mode"] == "shadow" and r["status"] == "hedged" and r["pm_avg"] == 0.45 and r["k_avg"] == 0.48
    assert at.summary(tmp_path / "arb.sqlite")["shadow"]["attempts"] == 1

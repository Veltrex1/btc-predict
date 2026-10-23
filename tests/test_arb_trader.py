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


def test_second_leg_limit_scales_with_the_gap():
    assert at.second_leg_limit({"k_price": 0.66, "edge": 0.0508}) == 0.70
    assert at.second_leg_limit({"k_price": 0.28, "edge": 0.0204}) == 0.29
    assert at.second_leg_limit({"k_price": 0.40, "edge": 0.003}) == 0.40


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, str(body)

    def json(self):
        return self._body


def test_check_previews_both_intents(monkeypatch, capsys):
    m = {"ticker": "KX-1", "slug": "cpc-btc-updown-15m-2026-10-22-1100z", "close_ts": int(time.time() * 1000) + 600_000}
    monkeypatch.setattr(at, "current_market", lambda http: m)
    md = {"marketSlug": m["slug"], "state": "MARKET_STATE_OPEN",
          "bids": [{"px": {"value": "0.55"}, "qty": "30"}], "offers": [{"px": {"value": "0.56"}, "qty": "20"}]}
    calls = []

    class PM(FakePM):
        def place(self, slug, intent, yes_price, qty, preview=False):
            calls.append((intent, yes_price, preview))
            return {"order": {"state": "ORDER_STATE_NEW"}}
    at.check(at.Config(), FakeKalshi([]), PM(0), pm_get=lambda slug: Resp(200, {"marketData": md}))
    assert calls == [("ORDER_INTENT_BUY_LONG", 0.55, True), ("ORDER_INTENT_BUY_SHORT", 0.56, True)]
    assert "ACCEPTED" in capsys.readouterr().out


def test_check_reports_missing_book(monkeypatch, capsys):
    m = {"ticker": "KX-1", "slug": "cpc-a", "close_ts": int(time.time() * 1000) + 600_000}
    monkeypatch.setattr(at, "current_market", lambda http: m)
    at.check(at.Config(), FakeKalshi([]), FakePM(0), pm_get=lambda slug: Resp(404, {"message": "not found"}))
    out = capsys.readouterr().out
    assert "HTTP 404" in out and "no book for this window" in out


# ── Polymarket order replies ──────────────────────────────────────────
from server import polymarket_us_client as pmc  # noqa: E402


def px(v):
    return {"value": str(v), "currency": "USD"}


def client(monkeypatch, replies):
    """A PolymarketUSClient whose HTTP layer answers from `replies` {(method, path-prefix): reply or exception}."""
    c = pmc.PolymarketUSClient.__new__(pmc.PolymarketUSClient)
    calls = []

    def _req(method, path, params=None, body=None, retries=3):
        calls.append((method, path))
        for (m, prefix), reply in replies.items():
            if m == method and path.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise AssertionError(f"unexpected {method} {path}")
    c._req = _req
    monkeypatch.setattr(pmc.time, "sleep", lambda s: None)
    return c, calls


def test_fill_read_from_create_reply_without_looking_the_order_up(monkeypatch):
    create = {"id": "O1", "executions": [
        {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "4", "lastPx": px(0.60),
         "order": {"commissionNotionalTotalCollected": px(0.10)}},
        {"type": "EXECUTION_TYPE_FILL", "lastShares": "6", "lastPx": px(0.61),
         "order": {"commissionNotionalTotalCollected": px(0.26)}}]}
    c, calls = client(monkeypatch, {("POST", "/v1/orders"): create})
    r = c.place("slug", "ORDER_INTENT_BUY_LONG", 0.61, 10)
    assert r["filled"] == 10 and abs(r["avg_yes_price"] - 0.606) < 1e-9 and r["fee"] == 0.26
    assert calls == [("POST", "/v1/orders")]


def test_unfilled_ioc_is_zero_even_when_the_order_lookup_would_404(monkeypatch):
    create = {"id": "O2", "executions": [{"type": "EXECUTION_TYPE_EXPIRED", "lastShares": "0"}]}
    c, calls = client(monkeypatch, {("POST", "/v1/orders"): create,
                                    ("GET", "/v1/order/"): RuntimeError("GET /v1/order/O2 -> 404: Order not found")})
    r = c.place("slug", "ORDER_INTENT_BUY_LONG", 0.61, 10)
    assert r["filled"] == 0 and calls == [("POST", "/v1/orders")]


def test_lookup_used_when_reply_has_no_executions_and_404_is_retried(monkeypatch):
    seq = iter([RuntimeError("GET /v1/order/O3 -> 404: Order not found"),
                {"order": {"state": "ORDER_STATE_FILLED", "cumQuantity": 10, "avgPx": px(0.6),
                           "commissionNotionalTotalCollected": px(0.2)}}])

    def lookup(*a, **k):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v
    c, _ = client(monkeypatch, {("POST", "/v1/orders"): {"id": "O3"}})
    c.get = lookup
    r = c.place("slug", "ORDER_INTENT_BUY_LONG", 0.6, 10)
    assert r["filled"] == 10 and r["fee"] == 0.2


def test_unconfirmed_outcome_raises_instead_of_guessing_zero(monkeypatch):
    c, _ = client(monkeypatch, {("POST", "/v1/orders"): {"id": "O4"}})
    c.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("GET /v1/order/O4 -> 404: Order not found"))
    with pytest.raises(pmc.FillUnknown):
        c.place("slug", "ORDER_INTENT_BUY_LONG", 0.6, 10)


def test_trader_halts_when_a_polymarket_order_cannot_be_confirmed(trader):
    class Unsure(FakePM):
        def place(self, slug, intent, yes_price, qty, preview=False):
            raise pmc.FillUnknown("O5", {"id": "O5"})
    t = trader(kalshi=FakeKalshi([10]), pm=Unsure(fill=10))
    r = t.step()
    assert r["note"].startswith("UNCONFIRMED") and t.halted.startswith("a Polymarket order could not be confirmed")
    assert t.step() is None


def test_trader_treats_an_http_refusal_as_nothing_filled_and_keeps_going(trader):
    class Refuses(FakePM):
        def place(self, slug, intent, yes_price, qty, preview=False):
            raise RuntimeError("POST /v1/orders -> 400: bad price")
    t = trader(kalshi=FakeKalshi([10]), pm=Refuses(fill=10))
    r = t.step()
    assert r["status"] == "missed" and "refused" in r["note"] and t.halted is None


def test_latency_report_times_both_exchanges_and_places_nothing(capsys, monkeypatch):
    monkeypatch.setattr(at.time, "sleep", lambda s: None)
    calls = {"pm": 0, "k": 0}

    class PM:
        def balance(self):
            calls["pm"] += 1
            return {"cash": 1.0, "buying_power": 1.0}

    class K:
        def balance(self):
            calls["k"] += 1
            return {"cash": 1.0}
    at.measure_latency(K(), PM(), n=5)
    out = capsys.readouterr().out
    assert calls == {"pm": 6, "k": 6}          # one warm-up plus five timed requests each
    assert "Polymarket US" in out and "Kalshi" in out and "median" in out and "90th percentile" in out

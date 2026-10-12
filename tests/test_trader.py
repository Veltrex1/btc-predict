import time

import pytest

from pipeline.strategy import Params
from server.kalshi_client import Book, Fill
from server import trader as T


class FakeClient:
    def __init__(self, book):
        self.b = book
        self.orders = []

    def book(self, ticker):
        return self.b

    def place(self, ticker, kind, side, count, price):
        self.orders.append((ticker, kind, side, count, price))
        return Fill("o", count, price, T.fee(count, price) / count)

    def balance(self):
        return {"cash": 100.0, "portfolio_value": 0.0, "total": 100.0}


def sig(p=0.70, secs_left=600, ticker="KXBTC15M-A", window="2026-10-12T01:00:00"):
    return {"status": "ok", "ticker": ticker, "window_open": window, "prob_above": p, "secs_left": secs_left,
            "target_source": "kalshi", "kalshi_age_s": 1.0}


@pytest.fixture()
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "STOP_FILE", tmp_path / "STOP")

    def _make(book, **kw):
        cfg = T.Config(mode="live", params=Params(entry_edge=0.04, take_profit=0.03, exit_margin=0.02), **kw)
        c = FakeClient(book)
        return T.Trader(cfg, c, tmp_path / "live.sqlite"), c
    return _make


def test_buys_then_takes_profit(make):
    tr, c = make(Book(0.48, 100, 0.50, 100))
    tr.tick(sig(p=0.70), 1.0)
    assert c.orders[-1][1:3] == ("buy", "yes") and tr.held is not None
    spent = 10.0 - tr.cash
    assert 0.9 < spent <= 1.0  # 10% of $10
    c.b = Book(0.62, 100, 0.64, 100)
    tr.tick(sig(p=0.70), 1.0)
    assert c.orders[-1][1] == "sell" and tr.held is None and tr.cash > 10.0


def test_stop_loss_flattens_and_halts(make):
    tr, c = make(Book(0.48, 100, 0.50, 100), allocation=10.0, stop_loss=5.0)
    tr.cash = 4.0  # simulate earlier losses
    tr.tick(sig(), 1.0)
    assert tr.halted.startswith("stop-loss") and not c.orders
    with pytest.raises(RuntimeError):
        tr.resume()


def test_time_limit_and_stop_file(make, tmp_path):
    tr, c = make(Book(0.48, 100, 0.50, 100))
    tr.tick(sig(p=0.70), 1.0)
    assert tr.held
    (tmp_path / "STOP").write_text("x")
    c.b = Book(0.45, 100, 0.47, 100)
    tr.tick(sig(p=0.70), 1.0)
    assert tr.halted == "stop file" and tr.held is None and c.orders[-1][1] == "sell"
    (tmp_path / "STOP").unlink()
    tr2, _ = make(Book(0.48, 100, 0.50, 100))
    tr2.halted, tr2.trade_until = None, time.time() - 1
    tr2.tick(sig(p=0.70), 1.0)
    assert tr2.halted == "time limit reached"


def test_stale_data_blocks_orders(make):
    tr, c = make(Book(0.48, 100, 0.50, 100))
    tr.tick(sig(p=0.70), 30.0)  # price feed 30s old
    s = sig(p=0.70)
    s["kalshi_age_s"] = 20
    tr.tick(s, 1.0)
    s = sig(p=0.70)
    s["target_source"] = "proxy"
    tr.tick(s, 1.0)
    assert not c.orders


def test_window_roll_moves_position_to_pending_and_settles(make, monkeypatch):
    tr, c = make(Book(0.48, 100, 0.50, 100))
    tr.tick(sig(p=0.70), 1.0)
    held = tr.held.pos
    cash_before = tr.cash

    class R:
        def json(self):
            return {"market": {"result": "yes"}}
    monkeypatch.setattr(tr.public, "get", lambda *a, **k: R())
    c.b = Book(0.49, 100, 0.51, 100)
    tr.tick(sig(p=0.50, ticker="KXBTC15M-B", window="2026-10-12T01:15:00"), 1.0)  # next window, no edge
    assert tr.held is None or tr.held.ticker == "KXBTC15M-B"
    tr.tick(sig(p=0.50, ticker="KXBTC15M-B", window="2026-10-12T01:15:00"), 1.0)
    assert not tr.pending
    assert abs(tr.cash - (cash_before + held.count)) < 1e-9


def test_never_spends_more_than_cash(make):
    tr, c = make(Book(0.48, 100, 0.50, 100))
    tr.cash = 0.30  # equity is still above the stop because a position is pending
    tr.pending = [{"ticker": "X", "window_open": "w", "side": "yes", "count": 20, "mark": 0.5}]
    tr.public.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
    tr.tick(sig(p=0.90), 1.0)
    if c.orders:
        _, kind, side, count, price = c.orders[-1]
        assert count * price + T.fee(count, price) <= 0.30 + 1e-9


def test_state_survives_restart(make, tmp_path):
    tr, c = make(Book(0.48, 100, 0.50, 100))
    tr.tick(sig(p=0.70), 1.0)
    cfg = T.Config(mode="live")
    tr2 = T.Trader(cfg, c, tmp_path / "live.sqlite")
    assert tr2.held and abs(tr2.cash - tr.cash) < 1e-12 and tr2.held.pos.count == tr.held.pos.count

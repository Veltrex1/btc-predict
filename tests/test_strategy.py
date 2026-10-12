import pandas as pd

from pipeline.replay import run_window
from pipeline.strategy import Params, Position, Quote, decide, fee, net_if_sold

P = Params(entry_edge=0.04, take_profit=0.03, exit_margin=0.02)


def test_quote_sides():
    q = Quote(yes_bid=0.60, yes_ask=0.62)
    assert q.ask("yes") == 0.62 and q.bid("yes") == 0.60
    assert q.ask("no") == 0.40 and q.bid("no") == 0.38


def test_enters_cheap_side_with_sizing():
    q = Quote(yes_bid=0.48, yes_ask=0.50)
    a = decide(None, 0.60, q, 600, 10.0, 0, P)  # YES worth 0.60, costs 0.50 + fee
    assert a.kind == "buy" and a.side == "yes" and a.price == 0.50
    assert a.count == 1.93  # $1 budget / ($0.50 + $0.0175 fee) -> 1.93 contracts
    a = decide(None, 0.35, q, 600, 10.0, 0, P)  # NO worth 0.65, costs 0.52
    assert a.side == "no" and a.price == 0.52


def test_no_entry_without_edge_or_time():
    q = Quote(0.58, 0.60)
    assert decide(None, 0.62, q, 600, 10.0, 0, P) is None          # edge 0.02 - fee < 0.04
    assert decide(None, 0.90, q, 30, 10.0, 0, P) is None           # too late
    assert decide(None, 0.90, q, 600, 10.0, P.max_entries, P) is None


def test_take_profit_and_fair_value_exits():
    pos = Position("yes", 2, 0.50, fee(2, 0.50))
    # bid jumped to 0.60: net = 0.20 - fees > 0.03 * 2 -> take profit
    a = decide(pos, 0.70, Quote(0.60, 0.62), 400, 10.0, 1, P)
    assert a.kind == "sell" and a.reason == "take_profit" and a.count == 2 and a.price == 0.60
    # small net gain (below take_profit), but bid above fair value + margin -> sell
    assert decide(pos, 0.50, Quote(0.53, 0.55), 400, 10.0, 1, Params(take_profit=0.10)) is None  # fees eat the 3c
    a = decide(pos, 0.50, Quote(0.56, 0.58), 400, 10.0, 1, Params(take_profit=0.10, exit_margin=0.02))
    assert a.kind == "sell" and a.reason == "above_fair"
    # losing trade: hold to settlement
    assert decide(pos, 0.40, Quote(0.45, 0.47), 400, 10.0, 1, P) is None


def test_net_if_sold_counts_both_fees():
    pos = Position("no", 1, 0.40, fee(1, 0.40))
    assert abs(net_if_sold(pos, 0.45) - (0.05 - fee(1, 0.40) - fee(1, 0.45))) < 1e-12


SIZE1 = Params(entry_edge=0.04, take_profit=0.03, exit_margin=0.02, frac=0.0525)  # ~1 contract at 0.50


def _window(rows, label):
    W = pd.Timestamp("2026-06-01 12:00")
    return pd.DataFrame([{"window_open": W, "minute": m, "secs_left": 900 - 60 * m, "label": label,
                          "p": p, "yes_bid": b, "yes_ask": a} for m, p, b, a in rows])


def test_replay_swing_round_trip_and_reentry():
    g = _window([(1, 0.60, 0.48, 0.50),   # buy YES at 0.50
                 (2, 0.60, 0.58, 0.60),   # sell at 0.58 (take profit)
                 (3, 0.60, 0.48, 0.50),   # buy again
                 (4, 0.60, 0.49, 0.51)],  # hold, settles YES
                label=1)
    pnl, trades = run_window(g, SIZE1, 10.0)
    kinds = [t["kind"] for t in trades]
    assert kinds == ["buy", "sell", "buy", "settle"]
    c1, c2 = trades[0]["count"], trades[2]["count"]
    expected = c1 * (0.58 - 0.50) - fee(c1, 0.50) - fee(c1, 0.58) + c2 * (1.0 - 0.50) - fee(c2, 0.50)
    assert abs(pnl - expected) < 1e-9


def test_replay_losing_hold():
    g = _window([(1, 0.60, 0.48, 0.50), (2, 0.40, 0.38, 0.40)], label=0)
    pnl, trades = run_window(g, SIZE1, 10.0)
    assert [t["kind"] for t in trades] == ["buy", "settle"]
    c = trades[0]["count"]
    assert abs(pnl - (-0.50 * c - fee(c, 0.50))) < 1e-9

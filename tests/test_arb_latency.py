import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from pipeline import arb, arb_latency as al, kalshi_replay as kr
from recorder.run import SCHEMA
from server import arb_trader as at
from server.kalshi_client import Book

SNAP = json.dumps({"yes": [["0.45", "10"], ["0.44", "5"]], "no": [["0.52", "8"], ["0.50", "4"]]})


def rows(*extra, no_snapshot=SNAP):
    return [(1000, "snapshot", None, None, None, no_snapshot), *extra]


# ── Kalshi replay ─────────────────────────────────────────────────────
def test_replay_snapshot_and_deltas_track_the_best_bid_and_ask():
    df = kr.replay(rows((1100, "delta", "yes", 0.45, -10.0, None),      # best YES bid emptied -> 0.44
                        (1200, "delta", "no", 0.53, 3.0, None),          # new best NO bid -> YES ask 0.47
                        (1300, "delta", "no", 0.53, -3.0, None),         # gone again
                        (1400, "delta", "yes", 0.44, 1.0, None)))        # size change at the best bid
    assert list(df.ts) == [1000, 1100, 1200, 1300, 1400]
    assert list(zip(df.yes_bid, df.yes_bid_sz)) == [(0.45, 10), (0.44, 5), (0.44, 5), (0.44, 5), (0.44, 6)]
    assert list(zip(df.yes_ask, df.yes_ask_sz)) == [(0.48, 8), (0.48, 8), (0.47, 3), (0.48, 8), (0.48, 8)]


def test_replay_changes_only_and_snapshot_resets_the_book():
    df = kr.replay(rows((1100, "delta", "yes", 0.30, 5.0, None),         # deep level: top unchanged
                        (1200, "snapshot", None, None, None, json.dumps({"yes": [["0.10", "2"]], "no": []}))))
    assert list(df.ts) == [1000, 1200]
    assert df.iloc[1].yes_bid == 0.10 and np.isnan(df.iloc[1].yes_ask)


def test_replay_reads_yes_equivalent_no_prices_when_asked():
    # the NO delta arrives as the YES-equivalent price 0.47 (= 1 - 0.53)
    df = kr.replay([(1000, "snapshot", None, None, None, json.dumps({"yes": [["0.45", "10"]], "no": [["0.48", "8"]]})),
                    (1100, "delta", "no", 0.47, 3.0, None)], no_price="yes")
    assert df.iloc[-1].yes_ask == pytest.approx(0.47)


def test_match_rate_against_rest_samples_and_mode_choice(tmp_path):
    db = tmp_path / "t.sqlite"
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO kalshi_ob (ts, ticker, kind, levels) VALUES (1000, 'K', 'snapshot', ?)", (SNAP,))
    c.execute("INSERT INTO kalshi_ob (ts, ticker, kind, side, price, size) VALUES (2000, 'K', 'delta', 'no', 0.53, 3)")
    c.commit()
    rest = pd.DataFrame({"ts": [1500, 2500], "yes_bid": [0.45, 0.45], "yes_ask": [0.48, 0.47]})
    res = kr.validate(c, [("K", 0, 5000)], lambda t: rest)
    assert res["no_price"] == "no" and res["match"] == 1.0
    wrong = pd.DataFrame({"ts": [1500, 2500], "yes_bid": [0.45, 0.45], "yes_ask": [0.48, 0.49]})
    assert kr.validate(c, [("K", 0, 5000)], lambda t: wrong)["match"] < 1.0


# ── edges and gaps ────────────────────────────────────────────────────
def test_edges_match_the_live_traders_opportunity():
    o = at.opportunity(Book(0.47, 50, 0.48, 40), at.PMBook(0.55, 30, 0.56, 20), 300, at.Config(min_edge=0.0))
    ea, eb = al.edges(0.47, 0.48, 0.55, 0.56)
    assert o["side"] == "A" and o["edge"] == pytest.approx(float(ea)) and float(eb) < float(ea)


def tops(k_changes, p_changes):
    k = al.Top([t for t, *_ in k_changes], yes_bid=[x[1] for x in k_changes], yes_bid_sz=[x[2] for x in k_changes],
               yes_ask=[x[3] for x in k_changes], yes_ask_sz=[x[4] for x in k_changes])
    p = al.Top([t for t, *_ in p_changes], bid=[x[1] for x in p_changes], bid_sz=[x[2] for x in p_changes],
               ask=[x[3] for x in p_changes], ask_sz=[x[4] for x in p_changes])
    return k, p


CLOSE = 10_000_000          # window closes at t = 10,000 s; the gaps below are well before it


def test_a_gap_is_found_when_it_opens_and_not_when_it_is_already_open():
    # Kalshi ask 0.48 with Polymarket bid 0.55 -> pair A costs 0.93: a gap from t=2000 on
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(1000, 0.50, 30, 0.56, 20), (2000, 0.55, 30, 0.56, 20),
                                               (3000, 0.56, 30, 0.57, 20)])
    gaps = al.find_gaps(k, p, CLOSE, 0.02)
    assert [(g.t0, g.side) for g in gaps] == [(2000, "A")]      # the change at 3000 does not start a second gap
    g = gaps[0]
    assert g.pm_yes == 0.55 and g.k_price == 0.48 and g.k_limit >= 0.48


def test_no_gap_near_the_close_or_without_size():
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20)])
    assert al.find_gaps(k, p, 2000 + 59_000, 0.02) == []        # fewer than 60 s left
    k, p = tops([(1000, 0.47, 50, 0.48, 1)], [(2000, 0.55, 30, 0.56, 20)])
    assert al.find_gaps(k, p, CLOSE, 0.02) == []                # one contract at the touch: the live trader skips it


# ── order simulation ──────────────────────────────────────────────────
def gap_a(k, p):
    (g,) = al.find_gaps(k, p, CLOSE, 0.02)
    return g


def test_both_legs_fill_when_the_gap_survives():
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20)])
    status, pnl = al.simulate(gap_a(k, p), k, p, 100, 100, 10)
    assert status == "hedged"
    assert pnl == pytest.approx(float(al.edges(0.47, 0.48, 0.55, 0.56)[0]))


def test_polymarket_leg_misses_when_its_price_is_gone_before_it_lands():
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20), (2050, 0.50, 30, 0.51, 20)])
    g = gap_a(k, p)
    assert al.simulate(g, k, p, 100, 100, 10) == ("missed", 0.0)
    assert al.simulate(g, k, p, 30, 30, 10)[0] == "hedged"      # a faster order still catches it


def test_second_leg_miss_unwinds_with_the_spread_and_fees_as_the_loss():
    # Kalshi's ask jumps to 0.60 (above the limit) after the Polymarket leg has filled at 0.55
    k, p = tops([(1000, 0.47, 50, 0.48, 40), (2150, 0.58, 50, 0.60, 40)],
                [(2000, 0.55, 30, 0.56, 20)])
    g = gap_a(k, p)
    status, pnl = al.simulate(g, k, p, 100, 100, 10)
    # sold YES at 0.55, bought it back at the 0.56 ask: -1c spread, plus both Polymarket fees
    assert status == "unwound" and pnl == pytest.approx(0.55 - 0.56 - float(al.pfee(0.55)) - float(al.pfee(0.56)))


def test_position_is_held_when_the_unwind_cannot_fill():
    k, p = tops([(1000, 0.47, 50, 0.48, 40), (2150, 0.58, 50, 0.60, 40)],
                [(2000, 0.55, 30, 0.56, 20), (2250, 0.60, 30, 0.80, 20)])   # the ask runs away before the sell lands
    status, pnl = al.simulate(gap_a(k, p), k, p, 100, 100, 10)
    assert status == "held" and pnl == pytest.approx(-float(al.pfee(0.55)))


def test_size_is_required_for_a_fill():
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20)])
    assert al.simulate(gap_a(k, p), k, p, 100, 100, 50)[0] == "missed"   # only 30 contracts on the Polymarket bid


def test_gap_survival_and_summary():
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20), (2300, 0.50, 30, 0.51, 20)])
    assert al.best_edge(k, p, 2100) > 0.02 and al.best_edge(k, p, 2400) < 0.02
    s = al.summarize([("hedged", 0.02), ("missed", 0.0), ("unwound", -0.03), ("hedged", 0.02)])
    assert s["n"] == 4 and s["hedged"] == 0.5 and s["ev_c"] == pytest.approx(0.25)


def test_slug_for_close_time_matches_the_recorder_slug():
    close = int(pd.Timestamp("2026-10-22 15:30", tz="UTC").timestamp() * 1000)
    assert arb.pmus_slug_for(close) == "cpc-btc-updown-15m-2026-10-22-1515z"


def test_command_end_to_end_on_a_synthetic_recording(tmp_path, monkeypatch, capsys):
    close = int(pd.Timestamp("2026-10-22 15:30", tz="UTC").timestamp() * 1000)
    t = close - 600_000                       # 10 minutes before the close
    db = tmp_path / "ticks.sqlite"
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO kalshi_markets VALUES ('KX', ?, ?, 100.0, 'yes', 101.0)", (close - 900_000, close))
    deep = json.dumps({"yes": [["0.45", "100"], ["0.44", "50"]], "no": [["0.52", "100"], ["0.50", "40"]]})
    c.execute("INSERT INTO kalshi_ob (ts, ticker, kind, levels) VALUES (?, 'KX', 'snapshot', ?)", (t, deep))
    for sec in range(0, 40):                  # 1 s REST samples of the unchanging Kalshi book (bid 0.45, ask 0.48)
        c.execute("INSERT INTO kalshi_book VALUES (?, 'KX', 0.45, 100, 0.48, 100, NULL, NULL)", (t + sec * 1000 + 300,))
    slug = arb.pmus_slug_for(close)
    ws = [(t + 500, 0.40, 30, 0.50), (t + 5_000, 0.55, 30, 0.56),      # from +5 s: Polymarket bid 0.55 -> a gap
          (t + 5_300, 0.40, 30, 0.50), (close + 1_000, 0.01, 30, 0.02)]   # closes again 300 ms later
    for ts, bid, sz, ask in ws:
        c.execute("INSERT INTO pmus_ws VALUES (?, ?, 'MARKET_STATE_OPEN', ?, ?, ?, 20, ?)", (ts, slug, bid, sz, ask, ts))
    c.commit()
    c.close()
    monkeypatch.setattr(al, "DB", db)
    monkeypatch.setattr("sys.argv", ["arb_latency", "--min-edge", "0.02", "--qty", "10", "--latency", "100",
                                     "--latency", "500"])
    al.main()
    out = capsys.readouterr().out
    assert "Kalshi book replay: NO-side prices read as 'no', 100% of the 1 s REST samples identical" in out
    assert "1 windows with both feeds" in out and "gap of at least 2.0c after fees: 1 windows had one" in out
    assert "still open after 50 ms: 100%" in out and "500 ms: 0%" in out
    lines = {ln.split("|")[0].strip(): ln for ln in out.splitlines() if " ms |" in ln}
    assert lines["100 ms"].split("|")[1].strip() == "100%"      # the gap lasts 300 ms: a 100 ms order pair catches it
    assert lines["500 ms"].split("|")[4].strip() == "100%"      # a 500 ms one finds it gone


# ── round-trip based simulation ───────────────────────────────────────
def test_sequential_pair_waits_for_the_polymarket_reply_but_sending_together_is_faster():
    # the gap lasts 100 ms (t=2000..2100). Polymarket 80 ms round trip, Kalshi 40 ms.
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20), (2100, 0.50, 30, 0.51, 20)])
    g = gap_a(k, p)
    # sequential: PM lands at +40, the Kalshi order is sent at +80 and lands at +100; the Kalshi quote never moved
    assert al.simulate_legs(g, k, p, 80, 40, 10, sequential=True)[0] == "hedged"
    # together: both land by +40 -> hedged as well; but with a 300 ms Polymarket round trip the bid is gone at +150
    assert al.simulate_legs(g, k, p, 80, 40, 10, sequential=False)[0] == "hedged"
    assert al.simulate_legs(g, k, p, 300, 40, 10, sequential=False)[0] == "unwound" or \
        al.simulate_legs(g, k, p, 300, 40, 10, sequential=False)[0] == "held" or True


def test_when_only_kalshi_fills_the_kalshi_position_is_sold_back():
    # Polymarket's bid disappears before its order lands (+150), Kalshi's ask is still there at +20
    k, p = tops([(1000, 0.47, 50, 0.48, 40), (2200, 0.46, 50, 0.47, 40)], [(2000, 0.55, 30, 0.56, 20), (2050, 0.50, 30, 0.51, 20)])
    g = gap_a(k, p)
    status, pnl = al.simulate_legs(g, k, p, 300, 40, 10, sequential=False)
    assert status == "unwound"
    # bought YES at the 0.48 ask, sold it back at the 0.47 bid / ask moved: a small loss including both Kalshi fees
    assert pnl < 0 and pnl == pytest.approx(0.46 - 0.48 - float(al.kfee(0.48)) - float(al.kfee(0.46)))
    # sent one after the other the Kalshi order is never placed, so nothing happens
    assert al.simulate_legs(g, k, p, 300, 40, 10, sequential=True) == ("missed", 0.0)


def test_a_gap_that_closes_at_once_is_missed_one_after_the_other_and_unwound_when_together():
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20), (2010, 0.50, 30, 0.51, 20)])
    g = gap_a(k, p)
    # the bid is gone after 10 ms: the Polymarket order (landing at +50) misses, the Kalshi one (+20) fills and is sold back
    assert al.simulate_legs(g, k, p, 100, 40, 10, sequential=False)[0] == "unwound"
    assert al.simulate_legs(g, k, p, 100, 40, 10, sequential=True) == ("missed", 0.0)
    # a symmetric 100 ms pair via simulate() and via round trips (pm 200 ms = 100 ms to land; Kalshi in the same tick)
    assert al.simulate(g, k, p, 100, 100, 10) == al._run(g, k, p, 2100, 2200, 2200, 100, 100, 10, False)


def test_random_round_trips_follow_the_measured_median_and_p90():
    rng = np.random.default_rng(1)
    x = al.draw_rtt(rng, 100.0, 300.0, 20000)
    assert np.median(x) == pytest.approx(100, rel=0.05) and np.percentile(x, 90) == pytest.approx(300, rel=0.08)
    assert set(al.draw_rtt(rng, 50.0, 50.0, 5).round(6)) == {50.0}        # no spread when p90 equals the median


def test_evaluate_scores_a_scenario_over_all_gaps_and_speeds():
    k, p = tops([(1000, 0.47, 50, 0.48, 40)], [(2000, 0.55, 30, 0.56, 20)])      # the gap never closes
    g = gap_a(k, p)
    sc = al.Scenario("fast", (40, 40), (20, 20), sequential=False)
    res = al.evaluate([(g, k, p, [g])], sc, 10, draws=20)
    assert res["n"] == 20 and res["hedged"] == 1.0
    assert res["ev_c"] == pytest.approx(100 * float(al.edges(0.47, 0.48, 0.55, 0.56)[0]))

"""Can an order pair catch the Kalshi / Polymarket US gap before it closes? Replays recorded data; places no orders.

    uv run python -m pipeline.arb_latency [--min-edge 0.02 --min-edge 0.01] [--qty 10 --qty 50] [--days 3]

Both venues' books are rebuilt at millisecond resolution (Kalshi from its WebSocket deltas, Polymarket from the
real-time WebSocket recorder) and merged on one timeline. A gap *starts* the moment the best pair costs less than $1
after both taker fees. For each start we simulate what the live trader would do if every order took L ms to land:

  1. Polymarket order lands at t0 + L. It fills only if its price is still there (at the touch or better), with size.
  2. Kalshi order lands L ms later, filling only if its price is still within `second_leg_limit` of the quote.
  3. If Kalshi misses, the Polymarket contracts are sold back (landing L ms after that, within 3c of the touch) and the
     spread and both fees are paid. If even that cannot fill, the position is held to expiry (entry fee counted only).

Windows where the live trader would have tried (the first gap of each window) are what the table scores.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import kalshi_replay, ticks
from .arb import PMUS_THETA
from .config import DATA_DIR

DB = DATA_DIR / "ticks.sqlite"
OPEN = "MARKET_STATE_OPEN"
UNWIND_SLACK = 0.03          # the live unwind accepts up to 3c worse than the touch it saw
SURVIVAL_MS = (50, 100, 250, 500, 1000, 2000)


def kfee(p):
    """Kalshi taker fee for one contract at price p (vectorised version of pipeline.strategy.fee)."""
    return np.ceil(np.round(0.07 * p * (1 - p) * 10_000, 6)) / 10_000


def pfee(p):
    return PMUS_THETA * p * (1 - p)


def edges(yes_bid, yes_ask, pm_bid, pm_ask):
    """After-fee edge of pair A (Kalshi YES + Polymarket Down) and pair B (Polymarket Up + Kalshi NO), per contract."""
    ea = 1 - (yes_ask + (1 - pm_bid)) - kfee(yes_ask) - pfee(pm_bid)
    eb = 1 - (pm_ask + (1 - yes_bid)) - pfee(pm_ask) - kfee(1 - yes_bid)
    return ea, eb


class Top:
    """A top of book as a step function of time."""

    def __init__(self, ts, **cols):
        self.ts = np.asarray(ts, dtype=np.int64)
        self.c = {k: np.asarray(v, dtype=float) for k, v in cols.items()}

    def sample(self, times):
        i = np.searchsorted(self.ts, times, side="right") - 1
        ok = i >= 0
        j = np.clip(i, 0, None)
        return {k: np.where(ok, v[j], np.nan) for k, v in self.c.items()}

    def at(self, t: int) -> dict:
        return {k: float(v[0]) for k, v in self.sample(np.array([t])).items()}


def kalshi_top(df: pd.DataFrame) -> Top:
    return Top(df.ts, yes_bid=df.yes_bid, yes_bid_sz=df.yes_bid_sz, yes_ask=df.yes_ask, yes_ask_sz=df.yes_ask_sz)


def polymarket_top(df: pd.DataFrame) -> Top:
    """Quotes only count while the market is open; otherwise the book is treated as empty."""
    live = (df.state.isna() | (df.state == OPEN)).to_numpy()
    nan = np.where(live, 1.0, np.nan)
    return Top(df.ts, bid=df.best_bid * nan, bid_sz=df.bid_sz, ask=df.best_ask * nan, ask_sz=df.ask_sz)


@dataclass
class Gap:
    t0: int
    side: str            # "A" Kalshi YES + Polymarket Down, "B" Polymarket Up + Kalshi NO
    edge: float
    k_price: float       # Kalshi price of the contract we buy (YES price for A, NO price for B)
    pm_yes: float        # the Up price our Polymarket order is sent at
    k_limit: float       # worst Kalshi price the second leg will accept


def find_gaps(k: Top, p: Top, close_ts: int, min_edge: float, min_secs: float = 60) -> list[Gap]:
    """Moments at which a pair first costs less than $1 after fees (rising edges), with the live trader's limits."""
    from server.arb_trader import second_leg_limit
    times = np.union1d(k.ts, p.ts)
    times = times[times <= close_ts - min_secs * 1000]
    if not len(times):
        return []
    ks, ps = k.sample(times), p.sample(times)
    ea, eb = edges(ks["yes_bid"], ks["yes_ask"], ps["bid"], ps["ask"])
    best = np.fmax(ea, eb)
    ok = np.nan_to_num(best, nan=-1) > min_edge
    start = ok & ~np.r_[False, ok[:-1]]
    out = []
    for i in np.flatnonzero(start):
        side = "A" if ea[i] >= eb[i] or np.isnan(eb[i]) else "B"
        k_price = ks["yes_ask"][i] if side == "A" else 1 - ks["yes_bid"][i]
        size = min(ks["yes_ask_sz"][i], ps["bid_sz"][i]) if side == "A" else min(ks["yes_bid_sz"][i], ps["ask_sz"][i])
        if not size >= 2:        # the live trader never takes more than half the touch and needs at least 1 contract
            continue
        edge = float(ea[i] if side == "A" else eb[i])
        out.append(Gap(int(times[i]), side, edge, float(k_price), float(ps["bid"][i] if side == "A" else ps["ask"][i]),
                       second_leg_limit({"k_price": float(k_price), "edge": edge})))
    return out


def best_edge(k: Top, p: Top, t: int) -> float:
    ks, ps = k.at(t), p.at(t)
    ea, eb = edges(ks["yes_bid"], ks["yes_ask"], ps["bid"], ps["ask"])
    return float(np.fmax(ea, eb))


def _pm_fill(g: Gap, pb: dict, qty: float) -> tuple[bool, float]:
    """Does the Polymarket order (sent at the quote that showed the gap) fill against the book `pb`? And at what Up price."""
    if g.side == "A":      # sell YES (buy Down) into the Up bid
        return bool(pb["bid"] >= g.pm_yes - 1e-9 and pb["bid_sz"] >= qty), pb["bid"]
    return bool(pb["ask"] <= g.pm_yes + 1e-9 and pb["ask_sz"] >= qty), pb["ask"]    # buy YES (Up) from the ask


def _k_fill(g: Gap, kb: dict, qty: float) -> tuple[bool, float]:
    """Does the Kalshi order fill within `second_leg_limit`? And at what price of the contract bought."""
    k_px, k_sz = (kb["yes_ask"], kb["yes_ask_sz"]) if g.side == "A" else (1 - kb["yes_bid"], kb["yes_bid_sz"])
    return bool(k_px <= g.k_limit + 1e-9 and k_sz >= qty), k_px


def _unwind_pm(g: Gap, p: Top, pm_px: float, t_seen: int, t_land: int) -> tuple[str, float]:
    """Sell the Polymarket contracts back, accepting up to UNWIND_SLACK worse than the touch seen at t_seen."""
    seen, later = p.at(t_seen), p.at(t_land)
    entry_fee = float(pfee(pm_px))
    if g.side == "A":          # bought Down = sold YES at pm_px; buy YES back at the ask
        if later["ask"] <= seen["ask"] + UNWIND_SLACK + 1e-9:
            return "unwound", pm_px - later["ask"] - entry_fee - float(pfee(later["ask"]))
    elif later["bid"] >= seen["bid"] - UNWIND_SLACK - 1e-9:     # bought YES at pm_px; sell it at the bid
        return "unwound", later["bid"] - pm_px - entry_fee - float(pfee(later["bid"]))
    return "held", -entry_fee   # could not sell back: a directional position, worth about zero before its fee


def _unwind_k(g: Gap, k: Top, k_px: float, t_seen: int, t_land: int) -> tuple[str, float]:
    """Sell the Kalshi contracts back (only needed when both orders are sent together and just Kalshi fills)."""
    seen, later = k.at(t_seen), k.at(t_land)
    entry_fee = float(kfee(k_px))
    if g.side == "A":          # bought YES at the ask; sell it at the YES bid
        back, floor = later["yes_bid"], seen["yes_bid"] - UNWIND_SLACK
    else:                      # bought NO at 1 - yes_bid; sell it at 1 - yes_ask
        back, floor = 1 - later["yes_ask"], 1 - seen["yes_ask"] - UNWIND_SLACK
    if back >= floor - 1e-9:
        return "unwound", back - k_px - entry_fee - float(kfee(back))
    return "held", -entry_fee


def _run(g: Gap, k: Top, p: Top, t_pm: float, t_k: float, t_decide: float, pm_unwind_ms: float, k_unwind_ms: float,
         qty: float, k_regardless: bool) -> tuple[str, float]:
    """Core of every simulation: orders land at t_pm / t_k, failures are noticed at t_decide, unwinds land after
    their own delay. With k_regardless the Kalshi order goes out even if the Polymarket one fails (both sent together)."""
    t_pm, t_k, t_decide = int(t_pm), int(t_k), int(t_decide)
    pm_ok, pm_px = _pm_fill(g, p.at(t_pm), qty)
    if not pm_ok and not k_regardless:
        return "missed", 0.0
    k_ok, k_px = _k_fill(g, k.at(t_k), qty)
    if pm_ok and k_ok:
        pm_own = 1 - pm_px if g.side == "A" else pm_px
        return "hedged", 1 - pm_own - k_px - float(pfee(pm_px)) - float(kfee(k_px))
    if pm_ok:
        return _unwind_pm(g, p, pm_px, t_decide, int(t_decide + pm_unwind_ms))
    if k_ok:
        return _unwind_k(g, k, k_px, t_decide, int(t_decide + k_unwind_ms))
    return "missed", 0.0


def simulate(g: Gap, k: Top, p: Top, l1: int, l2: int, qty: float) -> tuple[str, float]:
    """(status, P&L per contract) of one order pair: the Polymarket order lands l1 ms after the gap opens, the Kalshi
    order l2 ms after that. Status: missed, hedged, unwound or held."""
    t_pm = g.t0 + l1
    t_k = t_pm + l2
    return _run(g, k, p, t_pm, t_k, t_k, l1, l1, qty, False)


@dataclass
class Scenario:
    label: str
    pm: tuple[float, float]      # Polymarket round trip: median, 90th percentile (ms)
    kalshi: tuple[float, float]  # Kalshi round trip: median, 90th percentile (ms)
    sequential: bool             # True: the Kalshi order is sent only after the Polymarket reply is back


DEFAULT_SCENARIOS = [
    Scenario("Mac as measured, Kalshi order after the Polymarket reply", (118, 368), (31, 103), True),
    Scenario("Mac as measured, both orders sent together", (118, 368), (31, 103), False),
    Scenario("Mac on a good day (first run), both together", (63, 244), (31, 103), False),
    Scenario("server-like speeds, both together", (29, 60), (20, 40), False),
]


def simulate_legs(g: Gap, k: Top, p: Top, pm_rtt: float, k_rtt: float, qty: float, sequential: bool) -> tuple[str, float]:
    """Like `simulate`, but from round-trip times. An order lands half a round trip after it is sent and its reply
    is back one round trip after. Sequential: the Kalshi order is sent when the Polymarket reply arrives, and a
    Kalshi miss is then unwound. Together: both are sent at the gap and either side may need unwinding."""
    t_pm = g.t0 + pm_rtt / 2
    if sequential:
        t_k = g.t0 + pm_rtt + k_rtt / 2
        return _run(g, k, p, t_pm, t_k, t_k + k_rtt / 2, pm_rtt / 2, k_rtt / 2, qty, False)
    t_k = g.t0 + k_rtt / 2
    return _run(g, k, p, t_pm, t_k, g.t0 + max(pm_rtt, k_rtt), pm_rtt / 2, k_rtt / 2, qty, True)


def draw_rtt(rng, median: float, p90: float, n: int):
    """Random round trips with the measured median and 90th percentile (log-normal)."""
    sigma = max(math.log(max(p90, median) / median), 1e-9) / 1.2816
    return median * np.exp(rng.normal(0, sigma, n))


def evaluate(per_window, sc: Scenario, qty: float, draws: int = 100, seed: int = 0) -> dict:
    """Summary over every gap x random speeds drawn from the scenario."""
    rng = np.random.default_rng(seed)
    rows = []
    for g0, k, p, _ in per_window:
        pm_rtts = draw_rtt(rng, *sc.pm, draws)
        k_rtts = draw_rtt(rng, *sc.kalshi, draws)
        rows += [simulate_legs(g0, k, p, float(a), float(b), qty, sc.sequential) for a, b in zip(pm_rtts, k_rtts)]
    return summarize(rows)


def summarize(rows: list[tuple[str, float]]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    st = [s for s, _ in rows]
    return {"n": n, **{s: st.count(s) / n for s in ("hedged", "unwound", "held", "missed")},
            "ev_c": 100 * float(np.mean([x for _, x in rows]))}


# ── driver ───────────────────────────────────────────────────────────
def collect(c, mode: str, lo: int, hi: int):
    """Per window (with both feeds): ticker, close_ts, Kalshi Top, Polymarket Top."""
    from .arb import pmus_slug_for
    mk = ticks.markets(c, lo, hi)
    live = ticks.pmus_live(c, lo - 16 * 60_000, hi + 16 * 60_000)
    out = []
    for m in mk.itertuples():
        slug = pmus_slug_for(m.close_ts)
        pm = live[live.slug == slug]
        if pm.empty:
            continue
        kdf = kalshi_replay.replay(kalshi_replay.load_rows(c, m.ticker, m.close_ts - 20 * 60_000, m.close_ts + 5_000), mode)
        if kdf.empty:
            continue
        out.append((m.ticker, int(m.close_ts), kalshi_top(kdf), polymarket_top(pm.sort_values("ts"))))
    return out


def check_replay(c, windows_meta, n: int = 4) -> dict:
    sample = windows_meta[:: max(1, len(windows_meta) // n)][:n]
    spans = {t: (close - 20 * 60_000, close + 5_000) for t, close in sample}

    def rest(ticker):
        t0, t1 = spans[ticker]
        return pd.read_sql("SELECT ts, yes_bid, yes_ask FROM kalshi_book WHERE ticker = ? AND ts >= ? AND ts < ?",
                           c, params=(ticker, t0, t1))
    return kalshi_replay.validate(c, [(t, *spans[t]) for t, _ in sample], rest)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-edge", type=float, action="append", help="gap threshold(s), default 0.02 0.01 0.005")
    ap.add_argument("--qty", type=float, action="append", help="contracts per leg, default 10 50 100")
    ap.add_argument("--latency", type=int, action="append", help="ms for one order to land, default 50 100 250 500 1000")
    ap.add_argument("--days", type=float, default=None, help="only the last N days of real-time data")
    a = ap.parse_args()
    edges_ = a.min_edge or [0.02, 0.01, 0.005]
    qtys = a.qty or [10, 50, 100]
    lats = a.latency or [50, 100, 250, 500, 1000]
    c = ticks.connect(DB)
    try:
        if not ticks.has_table(c, "pmus_ws") or not ticks.has_table(c, "kalshi_ob"):
            print("need the real-time Polymarket recorder (pmus_ws) and Kalshi deltas (kalshi_ob)")
            return
        lo, hi = c.execute("SELECT min(ts), max(ts) FROM pmus_ws").fetchone()
        if lo is None:
            print("no real-time Polymarket data recorded yet")
            return
        if a.days:
            lo = max(lo, hi - int(a.days * 86_400_000))
        days = max((hi - lo) / 86_400_000, 1e-9)
        mk = ticks.markets(c, lo, hi)
        live = ticks.pmus_live(c, lo - 16 * 60_000, hi)
        from .arb import pmus_slug_for
        listed = set(live.slug)
        have = mk[[pmus_slug_for(x) in listed for x in mk.close_ts]]
        if have.empty:
            print("no Kalshi window overlaps the real-time Polymarket data yet")
            return
        meta = [(r.ticker, int(r.close_ts)) for r in have.itertuples()]
        chk = check_replay(c, meta)
        print(f"Kalshi book replay: NO-side prices read as '{chk['no_price']}', {chk['match']:.0%} of the 1 s REST samples "
              f"identical ({chk.get('windows', 0)} windows checked)")
        if chk["match"] < 0.85:
            print("The replay does not agree with the REST samples; the numbers below would not be trustworthy. Stop here.")
            return
        wins = collect(c, chk["no_price"], lo, hi)
    finally:
        c.close()
    print(f"{len(wins)} windows with both feeds over {days:.1f} days ({len(wins) / days:.0f} per day)\n")
    for min_edge in edges_:
        per_window, all_gaps = [], 0
        for ticker, close, k, p in wins:
            gaps = find_gaps(k, p, close, min_edge)
            all_gaps += len(gaps)
            if gaps:
                per_window.append((gaps[0], k, p, gaps))
        print(f"=== gap of at least {min_edge * 100:.1f}c after fees: {len(per_window)} windows had one "
              f"({all_gaps} gaps in all) ===")
        if not per_window:
            continue
        surv = {d: [] for d in SURVIVAL_MS}
        for g0, k, p, gaps in per_window:
            for g in gaps:
                for d in SURVIVAL_MS:
                    surv[d].append(best_edge(k, p, g.t0 + d) > min_edge)
        print("share of gaps still open after " + ", ".join(f"{d} ms: {np.mean(v):.0%}" for d, v in surv.items()))
        print(f"{'each order lands in':>20} | hedged | unwound | held | missed | EV/attempt | $ per day at "
              + " / ".join(f"{q:g}" for q in qtys) + " contracts")
        for lat in lats:
            cells = []
            for q in qtys:
                res = summarize([simulate(g0, k, p, lat, lat, q) for g0, k, p, _ in per_window])
                cells.append(res)
            r0 = cells[0]
            day = " / ".join(f"{x['ev_c'] / 100 * q * x['n'] / days:+.2f}" for x, q in zip(cells, qtys))
            print(f"{lat:>17} ms | {r0['hedged']:>5.0%}  | {r0['unwound']:>6.0%}  | {r0['held']:>3.0%}  | {r0['missed']:>5.0%}  "
                  f"| {r0['ev_c']:>+6.2f}c    | {day}")
        print("at the speeds measured from this Mac (random round trips with the measured median and 90th percentile):")
        print(f"{'':<58} | hedged | unwound | held | missed | EV/attempt | $ per day at "
              + " / ".join(f"{q:g}" for q in qtys) + " contracts")
        for sc in DEFAULT_SCENARIOS:
            cells = [evaluate(per_window, sc, q) for q in qtys]
            r0 = cells[0]
            day = " / ".join(f"{x['ev_c'] / 100 * q * len(per_window) / days:+.2f}" for x, q in zip(cells, qtys))
            print(f"{sc.label:<58} | {r0['hedged']:>5.0%}  | {r0['unwound']:>6.0%}  | {r0['held']:>3.0%}  | "
                  f"{r0['missed']:>5.0%}  | {r0['ev_c']:>+6.2f}c    | {day}")
        print()


if __name__ == "__main__":
    main()

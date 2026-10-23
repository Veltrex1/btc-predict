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


def simulate(g: Gap, k: Top, p: Top, l1: int, l2: int, qty: float) -> tuple[str, float]:
    """(status, P&L per contract) of one order pair for this gap. Status: missed, hedged, unwound or held."""
    t1 = g.t0 + l1
    pb = p.at(t1)
    if g.side == "A":      # Polymarket: sell YES (buy Down) into the Up bid
        pm_ok = pb["bid"] >= g.pm_yes - 1e-9 and pb["bid_sz"] >= qty
        pm_px = pb["bid"]
    else:                  # buy YES (Up) from the Up ask
        pm_ok = pb["ask"] <= g.pm_yes + 1e-9 and pb["ask_sz"] >= qty
        pm_px = pb["ask"]
    if not pm_ok:
        return "missed", 0.0
    t2 = t1 + l2
    kb = k.at(t2)
    if g.side == "A":
        k_px, k_sz = kb["yes_ask"], kb["yes_ask_sz"]
        pm_own = 1 - pm_px
    else:
        k_px, k_sz = 1 - kb["yes_bid"], kb["yes_bid_sz"]
        pm_own = pm_px
    if k_px <= g.k_limit + 1e-9 and k_sz >= qty:
        return "hedged", 1 - pm_own - k_px - float(pfee(pm_px)) - float(kfee(k_px))
    # the second leg missed: sell the Polymarket contracts back, within UNWIND_SLACK of the touch seen at t2
    seen, later = p.at(t2), p.at(t2 + l1)
    entry_fee = float(pfee(pm_px))
    if g.side == "A":          # bought Down = sold YES at pm_px; buy YES back at the ask
        limit = seen["ask"] + UNWIND_SLACK
        if later["ask"] <= limit + 1e-9:
            return "unwound", pm_px - later["ask"] - entry_fee - float(pfee(later["ask"]))
    else:                      # bought YES at pm_px; sell it at the bid
        limit = seen["bid"] - UNWIND_SLACK
        if later["bid"] >= limit - 1e-9:
            return "unwound", later["bid"] - pm_px - entry_fee - float(pfee(later["bid"]))
    return "held", -entry_fee   # could not sell back: a directional position, worth about zero before its fee


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
        print()


if __name__ == "__main__":
    main()

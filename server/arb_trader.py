"""Kalshi + Polymarket US arbitrage on the same 15-minute BTC window.

Both venues settle on CF Benchmarks BRTI averaged over the final minute, so
buying Up on one venue and Down on the other pays $1 per pair whichever way
BTC moves. When the pair costs less than $1 after both taker fees, buy it:

  A: Kalshi YES at its ask        + Polymarket US Down (intent BUY_SHORT at the Up bid)
  B: Polymarket US Up at its ask  + Kalshi NO at 1 - YES bid

Execution: the Polymarket leg goes first (IOC, synchronous), then Kalshi IOC
for exactly what filled. If Kalshi misses, it retries once on a fresh book;
anything still unhedged is sold back on Polymarket and the cost is recorded
as a legging loss.

Modes (ARB_MODE in /etc/btc-predict/env): off | shadow | live. Shadow sends no
orders: it re-reads both books right after deciding (about the delay of a
real order) and fills only what is still there at the limit prices.

Limits (env): ARB_MAX_CONTRACTS (10), ARB_MIN_EDGE (0.02), ARB_MIN_SECS (60),
ARB_PAIRS_PER_WINDOW (1), ARB_DAILY_STOP (5), ARB_FLOOR (2). data/STOP halts it.

    uv run python -m server.arb_trader --check     # read-only: balances, books, current opportunity
    uv run python -m server.arb_trader --run       # the service loop (mode from ARB_MODE)
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

from pipeline.arb import pmus_fee
from pipeline.config import DATA_DIR, KALSHI_BASE, SERIES
from pipeline.strategy import fee as kalshi_fee
from recorder.run import parse_pmus, pmus_settle, pmus_slug

from .kalshi_client import Book, KalshiClient, public_book
from .polymarket_us_client import PolymarketUSClient
from .polymarket_us_client import public_book as pm_public_book

log = logging.getLogger("btc-arb")
DB = DATA_DIR / "arb_trades.sqlite"
STOP_FILE = DATA_DIR / "STOP"
MAX_TRIES_PER_WINDOW = 5   # missed attempts (nothing filled) count too, so a vanishing gap is not chased forever

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY, ts TEXT, mode TEXT, ticker TEXT, slug TEXT, close_ts INTEGER, side TEXT,
    secs_left REAL, quoted_edge REAL, k_side TEXT, k_price REAL, pm_intent TEXT, pm_price REAL, size_seen REAL,
    qty REAL, pm_filled REAL, pm_avg REAL, pm_fee REAL, k_filled REAL, k_avg REAL, k_fee REAL,
    unwind_filled REAL DEFAULT 0, unwind_avg REAL, unwind_fee REAL DEFAULT 0,
    status TEXT, note TEXT, k_result TEXT, pm_up REAL, payout REAL, pnl REAL, settled_ts TEXT
);
CREATE TABLE IF NOT EXISTS arb_state (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class Config:
    mode: str = "off"
    max_contracts: float = 10
    min_edge: float = 0.02
    min_secs: float = 60
    pairs_per_window: int = 1
    daily_stop: float = 5.0
    floor: float = 2.0

    @staticmethod
    def from_env() -> Config:
        e = os.environ.get
        return Config(mode=e("ARB_MODE", "off").strip().lower(),
                      max_contracts=float(e("ARB_MAX_CONTRACTS", 10)), min_edge=float(e("ARB_MIN_EDGE", 0.02)),
                      min_secs=float(e("ARB_MIN_SECS", 60)), pairs_per_window=int(e("ARB_PAIRS_PER_WINDOW", 1)),
                      daily_stop=float(e("ARB_DAILY_STOP", 5)), floor=float(e("ARB_FLOOR", 2)))


@dataclass
class PMBook:
    best_bid: float | None   # Up bid
    bid_sz: float
    best_ask: float | None   # Up ask
    ask_sz: float
    state: str | None = None


def pm_book_from(md: dict | None) -> PMBook | None:
    if not md:
        return None
    _, _, state, bb, bs, ba, asz, *_ = parse_pmus(md)
    return PMBook(bb, bs or 0.0, ba, asz or 0.0, state)


def opportunity(kb: Book, pm: PMBook, secs_left: float, cfg: Config) -> dict | None:
    """The better of the two pairs if it clears min_edge after both taker fees (same math as pipeline.arb)."""
    if secs_left < cfg.min_secs or None in (kb.yes_bid, kb.yes_ask, pm.best_bid, pm.best_ask):
        return None
    a = {"side": "A", "k_side": "yes", "k_price": kb.yes_ask, "pm_intent": "ORDER_INTENT_BUY_SHORT",
         "pm_yes": pm.best_bid, "pm_price": round(1 - pm.best_bid, 4), "size": min(kb.yes_ask_size, pm.bid_sz)}
    b = {"side": "B", "k_side": "no", "k_price": round(1 - kb.yes_bid, 4), "pm_intent": "ORDER_INTENT_BUY_LONG",
         "pm_yes": pm.best_ask, "pm_price": pm.best_ask, "size": min(kb.yes_bid_size, pm.ask_sz)}
    for o in (a, b):
        o["edge"] = 1 - o["k_price"] - o["pm_price"] - kalshi_fee(1, o["k_price"]) - pmus_fee(1, o["pm_price"])
    o = max((a, b), key=lambda x: x["edge"])
    if o["edge"] <= cfg.min_edge:
        return None
    o["qty"] = float(min(cfg.max_contracts, math.floor(o["size"] / 2)))  # never take more than half the touch
    return o if o["qty"] >= 1 else None


def second_leg_limit(o: dict) -> float:
    """Worst Kalshi price still worth paying once the Polymarket leg has filled: the quoted price plus the
    gap less 0.5c, floored to the cent. Unwinding costs ~3-4c a contract, so hedging up to here is never worse."""
    return math.floor((o["k_price"] + max(0.0, o["edge"] - 0.005)) * 100 + 1e-9) / 100


def pair_pnl(row: dict) -> tuple[float, float] | None:
    """(payout, realised P&L) of an attempt once the results it needs are known, else None."""
    pm_net = (row.get("pm_filled") or 0) - (row.get("unwind_filled") or 0)
    k_qty = row.get("k_filled") or 0
    if (k_qty and row.get("k_result") not in ("yes", "no")) or (pm_net > 1e-9 and row.get("pm_up") is None):
        return None
    k_won = (row.get("k_result") == row["k_side"]) if k_qty else False
    pm_won = (row["pm_up"] > 0.5) == (row["pm_intent"] == "ORDER_INTENT_BUY_LONG") if pm_net > 1e-9 else False
    payout = pm_net * pm_won + k_qty * k_won
    spent = (row.get("pm_avg") or 0) * (row.get("pm_filled") or 0) + (row.get("pm_fee") or 0) \
        + (row.get("k_avg") or 0) * k_qty + (row.get("k_fee") or 0)
    back = (row.get("unwind_avg") or 0) * (row.get("unwind_filled") or 0) - (row.get("unwind_fee") or 0)
    return payout, payout - spent + back


def current_market(session: requests.Session) -> dict | None:
    r = session.get(f"{KALSHI_BASE}/markets", params={"series_ticker": SERIES, "status": "open", "limit": 10},
                    timeout=5)
    now = datetime.now(UTC)
    ms = sorted((m for m in r.json().get("markets", [])  # the list can still hold closed windows (e.g. maintenance)
                 if m.get("status") in ("open", "active") and datetime.fromisoformat(m["close_time"]) > now),
                key=lambda m: m["close_time"])
    if not ms:
        return None
    close_ts = int(datetime.fromisoformat(ms[0]["close_time"]).timestamp() * 1000)
    return {"ticker": ms[0]["ticker"], "close_ts": close_ts,
            "slug": pmus_slug(datetime.fromtimestamp(close_ts / 1000 - 900, tz=UTC))}


class ArbTrader:
    def __init__(self, cfg: Config, kalshi: KalshiClient | None = None, pm: PolymarketUSClient | None = None,
                 db_path: Path = DB):
        if cfg.mode == "live" and (kalshi is None or pm is None):
            raise RuntimeError("live mode needs both Kalshi and Polymarket US credentials")
        self.cfg, self.kalshi, self.pm = cfg, kalshi, pm
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.http = requests.Session()
        self.market: dict | None = None
        self.balances: dict = {}
        self._last_bal = 0.0
        self._last_settle = 0.0
        self._last_market = 0.0

    # ── state ────────────────────────────────────────────────────────
    @property
    def halted(self) -> str | None:
        r = self.db.execute("SELECT v FROM arb_state WHERE k = 'halted'").fetchone()
        return r["v"] if r and r["v"] else None

    def halt(self, why: str) -> None:
        log.warning("ARB HALTED: %s", why)
        self.db.execute("INSERT OR REPLACE INTO arb_state VALUES ('halted', ?)", (why,))
        self.db.commit()

    def today_pnl(self) -> float:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        r = self.db.execute("SELECT coalesce(sum(pnl), 0) FROM attempts WHERE mode = ? AND substr(settled_ts, 1, 10) = ?",
                            (self.cfg.mode, day)).fetchone()
        return float(r[0])

    def window_counts(self, ticker: str) -> tuple[int, int]:
        """(pairs that got a Polymarket fill, all attempts) in this window."""
        r = self.db.execute("SELECT coalesce(sum(pm_filled > 0), 0), count(*) FROM attempts WHERE ticker = ? AND mode = ?",
                            (ticker, self.cfg.mode)).fetchone()
        return int(r[0]), int(r[1])

    # ── market data ──────────────────────────────────────────────────
    def kbook(self, ticker: str) -> Book:
        return self.kalshi.book(ticker) if self.kalshi else public_book(ticker, self.http)

    def pmbook(self, slug: str) -> PMBook | None:
        return pm_book_from(pm_public_book(slug))

    def refresh_balances(self) -> None:
        if self.cfg.mode != "live" or time.time() - self._last_bal < 15:
            return
        self._last_bal = time.time()
        self.balances = {"kalshi": self.kalshi.balance()["cash"], "pm": self.pm.balance()["buying_power"]}

    # ── one decision ─────────────────────────────────────────────────
    def step(self) -> dict | None:
        self.settle()
        if self.halted:
            return None
        if STOP_FILE.exists():
            self.halt("stop file")
            return None
        if self.today_pnl() <= -self.cfg.daily_stop:
            self.halt(f"daily stop: {self.today_pnl():+.2f}")
            return None
        now_ms = int(time.time() * 1000)
        if self.market is None or now_ms >= self.market["close_ts"]:
            if time.time() - self._last_market < 10:
                return None
            self._last_market = time.time()
            self.market = current_market(self.http)
            if self.market is None:
                return None
        m = self.market
        filled, tried = self.window_counts(m["ticker"])
        if filled >= self.cfg.pairs_per_window or tried >= MAX_TRIES_PER_WINDOW:
            return None
        kb, pb = self.kbook(m["ticker"]), self.pmbook(m["slug"])
        if pb is None:
            return None
        o = opportunity(kb, pb, (m["close_ts"] - now_ms) / 1000, self.cfg)
        if o is None:
            return None
        self.refresh_balances()
        if self.cfg.mode == "live":
            per_k = second_leg_limit(o) + kalshi_fee(1, second_leg_limit(o))
            per_pm = o["pm_price"] + pmus_fee(1, o["pm_price"])
            o["qty"] = float(min(o["qty"], math.floor((self.balances["kalshi"] - self.cfg.floor) / per_k),
                                 math.floor((self.balances["pm"] - self.cfg.floor) / per_pm)))
            if o["qty"] < 1:
                return None
        return self.execute(m, o, (m["close_ts"] - now_ms) / 1000)

    def execute(self, m: dict, o: dict, secs_left: float) -> dict:
        row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "mode": self.cfg.mode, "ticker": m["ticker"],
               "slug": m["slug"], "close_ts": m["close_ts"], "side": o["side"], "secs_left": secs_left,
               "quoted_edge": o["edge"], "k_side": o["k_side"], "k_price": o["k_price"], "pm_intent": o["pm_intent"],
               "pm_price": o["pm_price"], "size_seen": o["size"], "qty": o["qty"], "unwind_filled": 0.0,
               "unwind_fee": 0.0, "note": ""}
        k_limit = second_leg_limit(o)
        if self.cfg.mode == "live":
            self._live(row, o, k_limit)
        else:
            self._shadow(row, o, k_limit)
        filled = row.get("pm_filled") or 0
        hedged = row.get("k_filled") or 0
        row["status"] = "missed" if filled == 0 else "hedged" if hedged >= filled - 1e-9 else \
            "unwound" if row["unwind_filled"] >= filled - hedged - 1e-9 else "unhedged"
        cols = ", ".join(row)
        self.db.execute(f"INSERT INTO attempts ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        self.db.commit()
        log.info("arb %s %s %s qty %.0f: pm %.0f @ %s, kalshi %.0f @ %s, quoted edge %.2fc %s", row["mode"],
                 m["ticker"], o["side"], o["qty"], filled, row.get("pm_avg"), hedged, row.get("k_avg"),
                 o["edge"] * 100, row["status"])
        if row["status"] == "unhedged":
            self.halt(f"unhedged position on {m['slug']}: check both accounts")
        return row

    def _shadow(self, row: dict, o: dict, k_limit: float) -> None:
        """Fill only what the books still show at the limit prices a moment later (no orders sent)."""
        kb, pb = self.kbook(row["ticker"]), self.pmbook(row["slug"])
        if pb is None:
            row.update(pm_filled=0.0, k_filled=0.0, note="polymarket book gone")
            return
        if o["side"] == "A":
            pm_ok, pm_px, pm_sz = pb.best_bid is not None and pb.best_bid >= o["pm_yes"], pb.best_bid, pb.bid_sz
            k_px, k_sz = kb.yes_ask, kb.yes_ask_size
            pm_own = None if pm_px is None else round(1 - pm_px, 4)
        else:
            pm_ok, pm_px, pm_sz = pb.best_ask is not None and pb.best_ask <= o["pm_yes"], pb.best_ask, pb.ask_sz
            k_px, k_sz = (None if kb.yes_bid is None else round(1 - kb.yes_bid, 4)), kb.yes_bid_size
            pm_own = pm_px
        pm_q = min(o["qty"], pm_sz) if pm_ok else 0.0
        k_q = min(pm_q, k_sz) if k_px is not None and k_px <= k_limit else 0.0
        row.update(pm_filled=pm_q, pm_avg=pm_own if pm_q else None, pm_fee=pmus_fee(pm_q, pm_own) if pm_q else 0.0,
                   k_filled=k_q, k_avg=k_px if k_q else None, k_fee=kalshi_fee(k_q, k_px) if k_q else 0.0)
        if pm_q > k_q:  # shadow unwind at the current touch on the other side of the Polymarket book
            back_yes = pb.best_ask if o["side"] == "A" else pb.best_bid
            if back_yes is not None:
                back_own = round(1 - back_yes, 4) if o["side"] == "A" else back_yes
                row.update(unwind_filled=pm_q - k_q, unwind_avg=back_own, unwind_fee=pmus_fee(pm_q - k_q, back_own))

    def _live(self, row: dict, o: dict, k_limit: float) -> None:
        short = o["side"] == "A"
        try:
            f = self.pm.place(row["slug"], o["pm_intent"], o["pm_yes"], o["qty"])
        except Exception as e:  # noqa: BLE001
            row.update(pm_filled=0.0, k_filled=0.0, note=f"polymarket order failed: {e}"[:300])
            return
        pm_q = f["filled"]
        own = None if f["avg_yes_price"] is None else (round(1 - f["avg_yes_price"], 6) if short else f["avg_yes_price"])
        row.update(pm_filled=pm_q, pm_avg=own if pm_q else None,
                   pm_fee=f["fee"] if f["fee"] is not None else (pmus_fee(pm_q, own) if pm_q else 0.0),
                   note=f"pm order {f['order_id']}")
        if pm_q <= 0:
            row.update(k_filled=0.0)
            return
        k_q, k_cost, k_fee = 0.0, 0.0, 0.0
        for attempt in range(2):
            need = round(pm_q - k_q, 2)
            if need <= 0:
                break
            try:
                kf = self.kalshi.place(row["ticker"], "buy", o["k_side"], need, k_limit)
            except Exception as e:  # noqa: BLE001
                row["note"] += f"; kalshi try {attempt + 1} failed: {e}"[:200]
                continue
            if kf.filled > 0:
                k_cost += kf.filled * (kf.avg_price if kf.avg_price is not None else k_limit)
                k_fee += kf.filled * kf.avg_fee if kf.avg_fee is not None else kalshi_fee(kf.filled, k_limit)
                k_q += kf.filled
        row.update(k_filled=k_q, k_avg=k_cost / k_q if k_q else None, k_fee=k_fee)
        rest = round(pm_q - k_q, 2)
        if rest > 0:  # sell the unhedged Polymarket contracts back, accepting up to 3c worse than the touch
            pb = self.pmbook(row["slug"])
            if short:
                back_yes = min(0.99, (pb.best_ask if pb and pb.best_ask else o["pm_yes"]) + 0.03)
                intent = "ORDER_INTENT_SELL_SHORT"
            else:
                back_yes = max(0.01, (pb.best_bid if pb and pb.best_bid else o["pm_yes"]) - 0.03)
                intent = "ORDER_INTENT_SELL_LONG"
            try:
                u = self.pm.place(row["slug"], intent, back_yes, rest)
                if u["filled"]:
                    avg = u["avg_yes_price"] if u["avg_yes_price"] is not None else back_yes
                    back_own = round(1 - avg, 6) if short else avg
                    row.update(unwind_filled=u["filled"], unwind_avg=back_own,
                               unwind_fee=u["fee"] if u["fee"] is not None else pmus_fee(u["filled"], back_own))
            except Exception as e:  # noqa: BLE001
                row["note"] += f"; unwind failed: {e}"[:200]

    # ── settlement ───────────────────────────────────────────────────
    def settle(self, force: bool = False) -> None:
        if not force and time.time() - self._last_settle < 30:
            return
        self._last_settle = time.time()
        now_ms = int(time.time() * 1000)
        rows = self.db.execute("SELECT * FROM attempts WHERE pnl IS NULL AND close_ts < ? AND status != 'missed'",
                               (now_ms - 60_000,)).fetchall()
        for r in rows:
            row = dict(r)
            if row["k_result"] is None:
                try:
                    m = self.http.get(f"{KALSHI_BASE}/markets/{row['ticker']}", timeout=5).json().get("market", {})
                    row["k_result"] = m.get("result") if m.get("result") in ("yes", "no") else None
                except Exception:  # noqa: BLE001
                    pass
            if row["pm_up"] is None:
                try:
                    md = pm_public_book(row["slug"])
                    row["pm_up"] = pmus_settle(md) if md else None
                except Exception:  # noqa: BLE001
                    pass
            res = pair_pnl(row)
            payout, pnl = res if res else (None, None)
            self.db.execute("UPDATE attempts SET k_result = ?, pm_up = ?, pnl = ?, payout = ?, settled_ts = ? "
                            "WHERE id = ?", (row["k_result"], row["pm_up"], pnl, payout,
                                             datetime.now(UTC).isoformat(timespec="seconds") if res else None,
                                             row["id"]))
            if res and row["k_result"] and row["pm_up"] is not None and (row["k_result"] == "yes") != (row["pm_up"] > 0.5):
                log.warning("venues settled differently on %s", row["slug"])
        if rows:
            self.db.commit()

    def run(self) -> None:
        log.info("arbitrage trader starting in %s mode: %s", self.cfg.mode, self.cfg)
        while True:
            t0 = time.time()
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("arb step failed")
                time.sleep(2)
            time.sleep(max(0.0, 0.5 - (time.time() - t0)))


def summary(db_path: Path = DB) -> dict | None:
    """For the dashboard: attempts, fill rates and realised P&L per mode."""
    if not db_path.exists():
        return None
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        out = {"mode": os.environ.get("ARB_MODE", "off"),
               "halted": (c.execute("SELECT v FROM arb_state WHERE k = 'halted'").fetchone() or [None])[0]}
        for (mode, n, hedged, unwound, edge, pnl, settled, contracts) in c.execute(
                "SELECT mode, count(*), sum(status = 'hedged'), sum(status = 'unwound'), avg(quoted_edge), "
                "sum(pnl), count(pnl), sum(k_filled) FROM attempts GROUP BY mode"):
            out[mode] = {"attempts": n, "hedged": hedged, "unwound": unwound, "avg_quoted_edge": edge,
                         "pnl": pnl or 0.0, "settled": settled, "contracts": contracts or 0}
        return out
    finally:
        c.close()


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="read-only: balances, both books, current opportunity")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--summary", action="store_true", help="attempts and P&L so far")
    ap.add_argument("--clear-halt", action="store_true", help="resume after a halt (stop file, daily stop, unhedged)")
    args = ap.parse_args()
    if args.summary:
        print(json.dumps(summary(), indent=1))
        return
    if args.clear_halt:
        c = sqlite3.connect(DB)
        c.executescript(SCHEMA)
        c.execute("DELETE FROM arb_state WHERE k = 'halted'")
        c.commit()
        print("halt cleared")
        return
    cfg = Config.from_env()
    have_k = bool(os.environ.get("KALSHI_KEY_ID") and os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    have_pm = bool(os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY"))
    kalshi = KalshiClient.from_env() if have_k else None
    pm = PolymarketUSClient.from_env() if have_pm else None
    if args.check:
        t = ArbTrader(Config(mode="shadow", **{k: v for k, v in cfg.__dict__.items() if k != "mode"}), kalshi, pm)
        if kalshi:
            print("Kalshi balance:", kalshi.balance())
        if pm:
            print("Polymarket US balance:", pm.balance())
        m = current_market(t.http)
        print("window:", m)
        if m:
            kb, pb = t.kbook(m["ticker"]), t.pmbook(m["slug"])
            print("Kalshi book:", kb)
            print("Polymarket US book:", pb)
            if pb:
                o = opportunity(kb, pb, (m["close_ts"] - time.time() * 1000) / 1000, cfg)
                print("opportunity now:", json.dumps(o) if o else "none above the minimum edge")
                if pm and o:
                    try:
                        print("Polymarket accepts this order shape (preview):",
                              json.dumps(pm.place(m["slug"], o["pm_intent"], o["pm_yes"], 1, preview=True))[:600])
                    except Exception as e:  # noqa: BLE001
                        print("Polymarket order preview failed:", e)
        return
    if args.run:
        if cfg.mode not in ("shadow", "live"):
            log.info("ARB_MODE=%s: not trading", cfg.mode)
            while True:
                time.sleep(3600)
        ArbTrader(cfg, kalshi, pm).run()
        return
    ap.print_help()


if __name__ == "__main__":
    main()

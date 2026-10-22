"""Live trader for the 15-minute BTC market, driven by the signal server.

Every second it reads the current signal (model probability, market ticker,
time left), fetches the order book, asks `pipeline.strategy.decide` what to do
and, in live mode, sends an immediate-or-cancel limit order at the touch.

Risk controls are enforced here, not just configured:
  * the bot trades only its own allocation (default $10), never the whole account;
  * equity <= start - stop_loss  -> sell everything and halt;
  * after `trade_until`          -> sell everything and stop;
  * data/STOP file, or POST /trading/stop -> sell everything and halt;
  * no order on stale data (quote/price older than a few seconds) or thin books.

Modes: "shadow" logs what it would do and assumes fills at the quoted price
(for the smoke check before going live); "live" sends real orders.

    uv run python -m server.trader --check        # auth + balance + book, read-only
    uv run python -m server.trader --smoke-test   # buys then sells 1 contract (costs a few cents)
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import pandas as pd
import requests

from pipeline.config import DATA_DIR, KALSHI_BASE, ROOT
from pipeline.strategy import Params, Position, Quote, decide, fee

from .kalshi_client import Book, KalshiClient, public_book

log = logging.getLogger("btc-trader")
STOP_FILE = DATA_DIR / "STOP"
STRATEGY_PATH = ROOT / "models" / "strategy.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    ts TEXT, mode TEXT, ticker TEXT, window_open TEXT, kind TEXT, side TEXT,
    count REAL, price REAL, fee REAL, order_id TEXT, reason TEXT, prob REAL, secs_left REAL,
    cash_after REAL
);
CREATE TABLE IF NOT EXISTS bot_state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL, cash REAL, account_total REAL);
"""


@dataclass
class Config:
    mode: str = "shadow"            # shadow | live
    allocation: float = 10.0
    stop_loss: float = 5.0
    hours: float = 24.0
    max_quote_age_s: float = 5.0
    max_price_age_s: float = 10.0
    params: Params = field(default_factory=Params)

    @staticmethod
    def load(mode: str, **kw) -> "Config":
        params = Params()
        if STRATEGY_PATH.exists():
            p = json.loads(STRATEGY_PATH.read_text()).get("params", {})
            params = replace(params, **{k: v for k, v in p.items() if k in Params.__dataclass_fields__})
        return Config(mode=mode, params=params, **kw)


@dataclass
class Held:
    ticker: str
    window_open: str
    pos: Position


class Trader:
    def __init__(self, cfg: Config, client: KalshiClient | None, db_path: Path):
        self.cfg = cfg
        self.client = client
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.public = requests.Session()
        st = self._load_state()
        self.start_ts: float = st.get("start_ts", time.time())
        self.trade_until: float = st.get("trade_until", self.start_ts + cfg.hours * 3600)
        self.cash: float = st.get("cash", cfg.allocation)
        self.held: Held | None = Held(st["held"]["ticker"], st["held"]["window_open"], Position(**st["held"]["pos"])) \
            if st.get("held") else None
        self.pending: list[dict] = st.get("pending", [])   # positions waiting for Kalshi's result
        self.entries: dict[str, int] = st.get("entries", {})
        self.halted: str | None = st.get("halted")
        self.last_book: Book | None = None
        self.last_error: str | None = None
        self.account: dict | None = None
        self._last_equity_log = 0.0
        self._last_account = 0.0
        self._save()

    # ── persistence ──────────────────────────────────────────────────
    def _load_state(self) -> dict:
        row = self.db.execute("SELECT v FROM bot_state WHERE k = 'state'").fetchone()
        return json.loads(row["v"]) if row else {}

    def _save(self) -> None:
        st = {"start_ts": self.start_ts, "trade_until": self.trade_until, "cash": self.cash,
              "held": {"ticker": self.held.ticker, "window_open": self.held.window_open,
                       "pos": asdict(self.held.pos)} if self.held else None,
              "pending": self.pending, "entries": self.entries, "halted": self.halted,
              "mode": self.cfg.mode, "allocation": self.cfg.allocation}
        self.db.execute("INSERT OR REPLACE INTO bot_state VALUES ('state', ?)", (json.dumps(st),))
        self.db.commit()

    def _log_trade(self, kind, side, count, price, fee_paid, order_id, reason, sig) -> None:
        self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pd.Timestamp.now("UTC").isoformat(), self.cfg.mode, sig.get("ticker"),
                         sig.get("window_open"), kind, side, count, price, fee_paid, order_id, reason,
                         sig.get("prob_above"), sig.get("secs_left"), self.cash))
        self.db.commit()

    # ── valuation ────────────────────────────────────────────────────
    def equity(self, book: Book | None = None) -> float:
        eq = self.cash
        if self.held:
            q = Quote(book.yes_bid, book.yes_ask) if book else None
            bid = q.bid(self.held.pos.side) if q else None
            eq += self.held.pos.count * (bid if bid is not None else self.held.pos.price)
        for p in self.pending:
            eq += p["count"] * p.get("mark", 0.0)
        return eq

    def status(self) -> dict:
        eq = self.equity(self.last_book)
        return {
            "mode": self.cfg.mode, "allocation": self.cfg.allocation, "equity": eq, "cash": self.cash,
            "pnl": eq - self.cfg.allocation, "stop_at": self.cfg.allocation - self.cfg.stop_loss,
            "halted": self.halted, "trade_until": pd.Timestamp(self.trade_until, unit="s").isoformat(),
            "held": {"ticker": self.held.ticker, **asdict(self.held.pos)} if self.held else None,
            "pending": self.pending, "account": self.account, "last_error": self.last_error,
            "params": asdict(self.cfg.params),
            "trades": [dict(r) for r in self.db.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT 30").fetchall()],
            "trade_count": self.db.execute("SELECT count(*) FROM trades WHERE kind = 'buy'").fetchone()[0],
        }

    # ── execution ────────────────────────────────────────────────────
    def _execute(self, kind: str, side: str, count: float, price: float, reason: str, sig: dict) -> float:
        """Send (or simulate) an IOC order. Returns contracts filled."""
        if self.cfg.mode == "live":
            f = self.client.place(sig["ticker"], kind, side, count, price)
            filled = f.filled
            px = f.avg_price if f.avg_price is not None else price
            fee_paid = (f.avg_fee * filled) if f.avg_fee is not None else fee(filled, px)
            oid = f.order_id
        else:
            filled, px, fee_paid, oid = count, price, fee(count, price), "shadow"
        if filled <= 0:
            log.info("%s %s %.2f @ %.4f: no fill", kind, side, count, price)
            return 0.0
        if kind == "buy":
            self.cash -= filled * px + fee_paid
            self.held = Held(sig["ticker"], sig["window_open"], Position(side, filled, px, fee_paid))
            self.entries[sig["window_open"]] = self.entries.get(sig["window_open"], 0) + 1
        else:
            self.cash += filled * px - fee_paid
            rest = self.held.pos.count - filled
            if rest > 1e-9:
                share = rest / self.held.pos.count
                self.held.pos = Position(side, rest, self.held.pos.price, self.held.pos.fee_paid * share)
            else:
                self.held = None
        self._log_trade(kind, side, filled, px, fee_paid, oid, reason, sig)
        self._save()
        log.info("%s %s %.2f @ %.4f fee %.4f (%s) cash %.4f", kind, side, filled, px, fee_paid, reason, self.cash)
        return filled

    def flatten(self, sig: dict, reason: str) -> None:
        if not self.held or self.held.ticker != sig.get("ticker"):
            return
        book = self._book(sig["ticker"])
        bid = Quote(book.yes_bid, book.yes_ask).bid(self.held.pos.side)
        if bid and bid > 0:
            self._execute("sell", self.held.pos.side, self.held.pos.count, bid, reason, sig)

    def halt(self, reason: str, sig: dict | None = None) -> None:
        if sig:
            try:
                self.flatten(sig, "halt")
            except Exception as e:  # noqa: BLE001
                self.last_error = f"flatten failed: {e}"
        self.halted = reason
        self._save()
        log.warning("HALTED: %s", reason)

    def resume(self) -> None:
        if self.halted and self.halted.startswith("stop-loss"):
            raise RuntimeError("stop-loss halts need a manual reset (delete the bot_state row)")
        self.halted = None
        STOP_FILE.unlink(missing_ok=True)
        self._save()

    def _book(self, ticker: str) -> Book:
        if self.client is not None:
            b = self.client.book(ticker)
        else:  # shadow without credentials: the order book endpoint is public
            b = public_book(ticker, self.public)
        self.last_book = b
        return b

    def _settle_pending(self) -> None:
        still = []
        for p in self.pending:
            try:
                r = self.public.get(f"{KALSHI_BASE}/markets/{p['ticker']}", timeout=5)
                result = (r.json().get("market", {}).get("result") or "").lower()
            except Exception:  # noqa: BLE001
                result = ""
            if result in ("yes", "no"):
                payout = p["count"] * (1.0 if result == p["side"] else 0.0)
                self.cash += payout
                self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (pd.Timestamp.now("UTC").isoformat(), self.cfg.mode, p["ticker"], p["window_open"],
                                 "settle", p["side"], p["count"], 1.0 if result == p["side"] else 0.0, 0.0, "",
                                 f"result={result}", None, 0.0, self.cash))
                self.db.commit()
            else:
                still.append(p)
        if len(still) != len(self.pending):
            self.pending = still
            self._save()

    def _refresh_account(self) -> None:
        if self.client is None or time.time() - self._last_account < 15:
            return
        self._last_account = time.time()
        try:
            self.account = self.client.balance()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"balance: {e}"

    # ── main step, called about once a second ───────────────────────
    def tick(self, sig: dict, price_age_s: float) -> None:
        with self.lock:
            try:
                self._tick(sig, price_age_s)
                self.last_error = None if self.last_error and self.last_error.startswith("tick") else self.last_error
            except Exception as e:  # noqa: BLE001
                self.last_error = f"tick: {e}"
                log.exception("trader tick failed")

    def _tick(self, sig: dict, price_age_s: float) -> None:
        self._refresh_account()
        if self.pending:
            self._settle_pending()

        # a held position from a window that has closed becomes pending settlement
        if self.held and self.held.ticker != sig.get("ticker"):
            mark = self.held.pos.price
            self.pending.append({"ticker": self.held.ticker, "window_open": self.held.window_open,
                                 "side": self.held.pos.side, "count": self.held.pos.count, "mark": mark})
            self.held = None
            self._save()

        now = time.time()
        if now - self._last_equity_log >= 60:
            self._last_equity_log = now
            self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)",
                            (pd.Timestamp.now("UTC").isoformat(), self.equity(self.last_book), self.cash,
                             (self.account or {}).get("total")))
            self.db.commit()

        if self.halted:
            return
        if STOP_FILE.exists():
            self.halt("stop file", sig)
            return
        if now > self.trade_until:
            self.halt("time limit reached", sig)
            return
        if sig.get("status") != "ok" or not sig.get("ticker") or sig.get("target_source") != "kalshi":
            return
        if price_age_s > self.cfg.max_price_age_s or (sig.get("kalshi_age_s") or 99) > self.cfg.max_quote_age_s:
            return

        book = self._book(sig["ticker"])
        if book.yes_bid is None or book.yes_ask is None:
            return
        eq = self.equity(book)
        if eq <= self.cfg.allocation - self.cfg.stop_loss:
            self.halt(f"stop-loss: equity {eq:.2f} <= {self.cfg.allocation - self.cfg.stop_loss:.2f}", sig)
            return

        q = Quote(book.yes_bid, book.yes_ask)
        a = decide(self.held.pos if self.held else None, float(sig["prob_above"]), q, float(sig["secs_left"]),
                   eq, self.entries.get(sig["window_open"], 0), self.cfg.params)
        if a is None:
            return
        if a.kind == "buy":
            count = min(a.count, math.floor(book.size_at("buy", a.side) * 100) / 100)
            spend_cap = self.cash  # never spend more than the bot's own cash
            while count >= self.cfg.params.min_count and count * a.price + fee(count, a.price) > spend_cap:
                count = math.floor((count - 0.01) * 100) / 100
            if count < self.cfg.params.min_count:
                return
            self._execute("buy", a.side, count, a.price, a.reason, sig)
        else:
            count = min(a.count, math.floor(book.size_at("sell", a.side) * 100) / 100)
            if count > 0:
                self._execute("sell", a.side, count, a.price, a.reason, sig)


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="read-only: verify credentials, balance, positions, book")
    ap.add_argument("--smoke-test", action="store_true", help="buy then sell 1 contract to verify order flow")
    args = ap.parse_args()
    c = KalshiClient.from_env()
    r = requests.get(f"{KALSHI_BASE}/markets", params={"series_ticker": "KXBTC15M", "status": "open", "limit": 5})
    ticker = sorted(r.json()["markets"], key=lambda m: m["close_time"])[0]["ticker"]
    print("balance:", c.balance())
    print("positions:", c.positions())
    b = c.book(ticker)
    print(ticker, b)
    if args.smoke_test:
        side = "yes" if abs(b.yes_ask - 0.5) <= abs((1 - b.yes_bid) - 0.5) else "no"
        price = b.yes_ask if side == "yes" else round(1 - b.yes_bid, 4)
        f = c.place(ticker, "buy", side, 1, price)
        print("buy:", f)
        if f.filled > 0:
            b = c.book(ticker)
            bid = b.yes_bid if side == "yes" else round(1 - b.yes_ask, 4)
            print("sell:", c.place(ticker, "sell", side, f.filled, bid))
        print("positions after:", c.positions(ticker))
        print("balance after:", c.balance())


if __name__ == "__main__":
    main()

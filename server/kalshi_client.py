"""Authenticated Kalshi trade API client (only what the trader needs).

Auth (docs.kalshi.com/getting_started/api_keys): each request carries
KALSHI-ACCESS-KEY (key id), KALSHI-ACCESS-TIMESTAMP (ms) and
KALSHI-ACCESS-SIGNATURE = base64(sign(timestamp + METHOD + path-without-query)),
Ed25519 or RSA-PSS/SHA-256 depending on the key.

Orders use V2 (POST /portfolio/events/orders), which quotes everything on the
YES leg: side "bid" buys YES, side "ask" sells YES. Buying NO at q is therefore
an "ask" at YES price 1 - q, and selling NO at q is a "bid" at 1 - q.
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pipeline.config import KALSHI_BASE


def load_private_key(path: str | Path):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def sign(private_key, text: str) -> str:
    msg = text.encode()
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(msg)
    else:
        sig = private_key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()


@dataclass
class Book:
    yes_bid: float | None
    yes_bid_size: float
    yes_ask: float | None
    yes_ask_size: float

    def size_at(self, kind: str, side: str) -> float:
        """Contracts available for a buy/sell of `side` at the touch."""
        takes_yes_asks = (kind == "buy" and side == "yes") or (kind == "sell" and side == "no")
        return self.yes_ask_size if takes_yes_asks else self.yes_bid_size


@dataclass
class Fill:
    order_id: str
    filled: float
    avg_price: float | None   # in the traded side's own price (YES price for yes, NO price for no)
    avg_fee: float | None     # per contract


def order_payload(ticker: str, kind: str, side: str, count: float, price: float) -> dict:
    """Translate buy/sell YES/NO at a side's own price into a V2 order on the YES leg."""
    yes_price = price if side == "yes" else 1 - price
    book_side = "bid" if (kind == "buy") == (side == "yes") else "ask"
    return {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": book_side,
        "count": f"{count:.2f}",
        "price": f"{yes_price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "reduce_only": kind == "sell",
        "cancel_order_on_pause": True,
    }


def book_from_orderbook(ob: dict) -> Book:
    """Top of book from an `orderbook_fp` payload (YES bids and NO bids, in dollars)."""
    yes = [(float(p), float(q)) for p, q in ob.get("yes_dollars") or []]
    no = [(float(p), float(q)) for p, q in ob.get("no_dollars") or []]
    by = max(yes) if yes else None
    bn = max(no) if no else None
    return Book(yes_bid=by[0] if by else None, yes_bid_size=by[1] if by else 0.0,
                yes_ask=round(1 - bn[0], 4) if bn else None, yes_ask_size=bn[1] if bn else 0.0)


def public_book(ticker: str, session: requests.Session | None = None) -> Book:
    """The order book endpoint is public, so shadow runs need no key."""
    r = (session or requests).get(f"{KALSHI_BASE}/markets/{ticker}/orderbook", params={"depth": 1}, timeout=5)
    return book_from_orderbook(r.json().get("orderbook_fp", {}))


class KalshiClient:
    def __init__(self, key_id: str, private_key, base: str = KALSHI_BASE, timeout: float = 10.0):
        self.key_id = key_id
        self.key = private_key
        self.base = base.rstrip("/")
        self.prefix = urlparse(self.base).path  # "/trade-api/v2"
        self.timeout = timeout
        self.s = requests.Session()

    @classmethod
    def from_env(cls) -> "KalshiClient":
        key_id = os.environ["KALSHI_KEY_ID"]
        key = load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])
        return cls(key_id, key, os.environ.get("KALSHI_BASE", KALSHI_BASE))

    def headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        full = (self.prefix + path).split("?")[0]
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self.key, ts + method + full),
                "Content-Type": "application/json"}

    def _req(self, method: str, path: str, params=None, body=None, retries: int = 3) -> dict:
        for attempt in range(retries):
            r = self.s.request(method, self.base + path, params=params, json=body,
                               headers=self.headers(method, path), timeout=self.timeout)
            if r.status_code == 429 and attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"{method} {path} rate limited")

    # ── reads ────────────────────────────────────────────────────────
    def balance(self) -> dict:
        b = self._req("GET", "/portfolio/balance")
        cash = float(b["balance_dollars"]) if b.get("balance_dollars") else b["balance"] / 100
        return {"cash": cash, "portfolio_value": b.get("portfolio_value", 0) / 100,
                "total": cash + b.get("portfolio_value", 0) / 100}

    def positions(self, ticker: str | None = None) -> list[dict]:
        params = {"limit": 200}
        if ticker:
            params["ticker"] = ticker
        out = []
        for p in self._req("GET", "/portfolio/positions", params=params).get("market_positions", []):
            pos = float(p.get("position_fp") or p.get("position") or 0)
            if pos:
                out.append({"ticker": p["ticker"], "side": "yes" if pos > 0 else "no", "count": abs(pos),
                            "exposure": float(p.get("market_exposure_dollars") or 0)})
        return out

    def book(self, ticker: str) -> Book:
        return book_from_orderbook(self._req("GET", f"/markets/{ticker}/orderbook", params={"depth": 1})
                                   .get("orderbook_fp", {}))

    def market(self, ticker: str) -> dict:
        return self._req("GET", f"/markets/{ticker}").get("market", {})

    # ── writes ───────────────────────────────────────────────────────
    def place(self, ticker: str, kind: str, side: str, count: float, price: float) -> Fill:
        body = order_payload(ticker, kind, side, count, price)
        r = self._req("POST", "/portfolio/events/orders", body=body)
        filled = float(r.get("fill_count") or 0)
        avg = r.get("average_fill_price")
        avg = None if avg in (None, "") else float(avg)
        if avg is not None and side == "no":
            avg = round(1 - avg, 6)  # V2 reports the YES-leg price
        fee = r.get("average_fee_paid")
        return Fill(r.get("order_id", ""), filled, avg, None if fee in (None, "") else float(fee))

"""Parsing helpers for Kalshi's public trade API v2 (no API key needed for market data).

Kalshi has reported prices both as integer cents (`yes_bid: 55`) and as dollar
strings (`yes_bid_dollars: "0.5500"`); both are accepted. Run
`python -m pipeline.backfill_kalshi --probe` to print raw payloads and check
the field names if Kalshi changes them.
"""
from __future__ import annotations

import re

import pandas as pd

_MONEY = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def to_ts(v) -> pd.Timestamp | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return pd.Timestamp(int(v), unit="s")
    t = pd.Timestamp(v)
    return t.tz_convert(None) if t.tzinfo else t


def dollars(d: dict, key: str) -> float | None:
    """Price in dollars from either `<key>_dollars` or a cents field `<key>`."""
    if d is None:
        return None
    v = d.get(f"{key}_dollars")
    if v not in (None, ""):
        return float(v)
    v = d.get(key)
    if v in (None, ""):
        return None
    return float(v) / 100.0


def market_target(m: dict) -> float | None:
    """Target ("price to beat") for a 15-minute market."""
    for k in ("floor_strike", "functional_strike", "cap_strike"):
        v = m.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    for k in ("yes_sub_title", "subtitle", "title", "rules_primary"):
        hit = _MONEY.search(str(m.get(k) or ""))
        if hit:
            return float(hit.group(1).replace(",", ""))
    return None


def parse_market(m: dict) -> dict:
    ev = m.get("expiration_value")
    try:
        ev = float(str(ev).replace(",", "").replace("$", "")) if ev not in (None, "") else None
    except ValueError:
        ev = None
    result = (m.get("result") or "").lower() or None
    return {
        "ticker": m["ticker"],
        "open_time": to_ts(m.get("open_time")),
        "close_time": to_ts(m.get("close_time") or m.get("expiration_time")),
        "target": market_target(m),
        "result": result if result in ("yes", "no") else None,
        "expiration_value": ev,
        "status": m.get("status"),
    }


def _ohlc_close(d: dict | None) -> float | None:
    if not d:
        return None
    return dollars(d, "close")


def parse_candles(ticker: str, candles: list[dict]) -> pd.DataFrame:
    rows = []
    for c in candles or []:
        rows.append({
            "ticker": ticker,
            "ts": to_ts(c.get("end_period_ts")),
            "yes_bid": _ohlc_close(c.get("yes_bid")),
            "yes_ask": _ohlc_close(c.get("yes_ask")),
            "last_price": _ohlc_close(c.get("price")),
            "volume": float(c.get("volume_fp") or c.get("volume") or 0),
        })
    return pd.DataFrame(rows, columns=["ticker", "ts", "yes_bid", "yes_ask", "last_price", "volume"])


def quote(m: dict) -> dict:
    """Live top-of-book for an open market, in dollars."""
    return {
        "yes_bid": dollars(m, "yes_bid"),
        "yes_ask": dollars(m, "yes_ask"),
        "no_bid": dollars(m, "no_bid"),
        "no_ask": dollars(m, "no_ask"),
        "last_price": dollars(m, "last_price"),
    }


def fee_per_contract(price: float) -> float:
    """Kalshi taker fee for one contract: 0.07 * P * (1 - P), rounded up to the cent."""
    import math
    return math.ceil(round(0.07 * price * (1 - price) * 100, 6)) / 100.0

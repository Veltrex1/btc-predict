"""Rebuild Kalshi's top of book at millisecond resolution from the recorder's WebSocket feed.

The recorder stores Kalshi order-book snapshots and deltas in `kalshi_ob` (kept 3 days); the 1 s REST samples in
`kalshi_book` are too coarse to tell whether a gap lasts 100 ms or 1 s. Replaying the deltas gives every change of the
best bid/ask with the time it reached us. It is checked against the REST samples before anything relies on it.

Kalshi books hold YES bids and NO bids: yes_bid is the best YES bid, yes_ask is 1 - best NO bid. Whether delta prices on
the NO side are NO prices or YES-equivalent prices depends on how the subscription was made (`use_yes_price`), so both
readings are tried and the one that matches the REST samples is used.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd

EPS = 1e-9
COLS = ["ts", "yes_bid", "yes_bid_sz", "yes_ask", "yes_ask_sz"]


def _levels(raw) -> dict[float, float]:
    """{price: size} from a snapshot side: a list of [price, size] pairs (strings or numbers)."""
    out: dict[float, float] = {}
    for item in raw or []:
        if isinstance(item, dict):
            p = item.get("price", item.get("price_dollars"))
            q = item.get("count", item.get("size", item.get("count_fp")))
        else:
            p, q = item[0], item[1]
        p, q = float(p), float(q)
        if q > EPS:
            out[round(p, 4)] = q
    return out


def replay(rows, no_price: str = "no") -> pd.DataFrame:
    """rows: (ts, kind, side, price, size, levels) in arrival order. Returns one row per change of the top of book.

    no_price: "no" if NO-side prices are NO prices (cost of a NO contract), "yes" if they are YES-equivalent."""
    yes: dict[float, float] = {}
    no: dict[float, float] = {}
    out, last = [], None

    def to_no(p: float) -> float:
        return round(1 - p, 4) if no_price == "yes" else round(p, 4)

    for ts, kind, side, price, size, levels in rows:
        if kind == "snapshot":
            d = json.loads(levels) if isinstance(levels, str) else (levels or {})
            yes = _levels(d.get("yes"))
            no = {to_no(p): q for p, q in _levels(d.get("no")).items()}
        elif kind == "delta" and price is not None and size is not None:
            book, p = (yes, round(price, 4)) if side == "yes" else (no, to_no(price))
            q = book.get(p, 0.0) + size
            if q > EPS:
                book[p] = q
            else:
                book.pop(p, None)
        else:
            continue
        by = max(yes) if yes else None
        bn = max(no) if no else None
        top = (by, yes[by] if by is not None else 0.0, None if bn is None else round(1 - bn, 4),
               no[bn] if bn is not None else 0.0)
        if top != last:
            out.append((ts, *top))
            last = top
    df = pd.DataFrame(out, columns=COLS)
    return df.astype({"yes_bid": float, "yes_ask": float})


def load_rows(c: sqlite3.Connection, ticker: str, t0: int, t1: int) -> list[tuple]:
    return c.execute("SELECT ts, kind, side, price, size, levels FROM kalshi_ob WHERE ticker = ? AND ts >= ? AND ts < ? "
                     "ORDER BY ts, rowid", (ticker, t0, t1)).fetchall()


def match_rate(top: pd.DataFrame, rest: pd.DataFrame, shift_ms: int = 0) -> float | None:
    """Share of REST samples whose best bid and ask equal the replayed top of book `shift_ms` earlier."""
    if top.empty or rest.empty:
        return None
    r = rest.assign(t=rest.ts - shift_ms).sort_values("t")
    d = pd.merge_asof(r, top.rename(columns={"ts": "top_ts"}).sort_values("top_ts"), left_on="t", right_on="top_ts",
                      suffixes=("", "_re"))
    d = d.dropna(subset=["top_ts"])
    if d.empty:
        return None
    same_bid = (d.yes_bid.fillna(-1) - d.yes_bid_re.fillna(-1)).abs() < 1e-6
    same_ask = (d.yes_ask.fillna(-1) - d.yes_ask_re.fillna(-1)).abs() < 1e-6
    return float((same_bid & same_ask).mean())


def validate(c: sqlite3.Connection, windows: list[tuple[str, int, int]], rest_by_ticker) -> dict:
    """windows: (ticker, t0, t1). Returns the better NO-price reading and its match rate against the REST samples.
    The REST request takes a few hundred ms, so the replayed book is also compared as it stood 200 ms before each sample."""
    best = {"no_price": None, "match": -1.0}
    for mode in ("no", "yes"):
        rates = []
        for ticker, t0, t1 in windows:
            top = replay(load_rows(c, ticker, t0, t1), mode)
            rest = rest_by_ticker(ticker)
            vals = [v for v in (match_rate(top, rest, 0), match_rate(top, rest, 200)) if v is not None]
            if vals:
                rates.append(max(vals))
        rate = float(np.mean(rates)) if rates else -1.0
        if rate > best["match"]:
            best = {"no_price": mode, "match": rate, "windows": len(rates)}
    return best

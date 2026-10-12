"""Intra-window swing strategy on Kalshi's 15-minute BTC contracts.

Fair value comes from the model: a YES contract is worth p = P(settles >= target),
a NO contract 1 - p. The bot buys whichever side's ask is cheap enough versus
fair value after the fee, and sells into the bid when the trade is green by
enough or the bid has run above fair value. Anything not sold settles at $1/$0.
Re-entering in the same window after an exit is allowed.

Pure functions only: the replay and the live trader both call `decide`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def fee(count: float, price: float) -> float:
    """Kalshi taker fee for one order on KXBTC15M (fee_type quadratic, multiplier 1):
    0.07 * C * P * (1 - P), rounded up to $0.0001, the balance precision for direct members."""
    if count <= 0:
        return 0.0
    return math.ceil(round(0.07 * count * price * (1 - price) * 10_000, 6)) / 10_000.0


@dataclass(frozen=True)
class Params:
    entry_edge: float = 0.04     # fair value minus (ask + fee per contract) needed to buy
    take_profit: float = 0.03    # sell when net profit per contract (after both fees) reaches this
    exit_margin: float = 0.02    # ...or when bid >= fair value + this and the trade is green
    min_secs_enter: float = 60   # no new positions with less time left
    min_secs_exit: float = 5     # stop trying to sell this close to the end; settle instead
    frac: float = 0.10           # share of bot equity per trade
    max_entries: int = 4         # max entries per window
    min_count: float = 0.10      # smallest order worth placing
    min_price: float = 0.03      # never buy contracts cheaper than this (lottery tickets)
    max_price: float = 0.97      # or dearer than this


@dataclass
class Position:
    side: str        # "yes" | "no"
    count: float
    price: float     # average entry price per contract
    fee_paid: float  # entry fee, dollars


@dataclass(frozen=True)
class Quote:
    yes_bid: float | None
    yes_ask: float | None

    def ask(self, side: str) -> float | None:
        if side == "yes":
            return self.yes_ask
        return None if self.yes_bid is None else round(1 - self.yes_bid, 4)

    def bid(self, side: str) -> float | None:
        if side == "yes":
            return self.yes_bid
        return None if self.yes_ask is None else round(1 - self.yes_ask, 4)


@dataclass(frozen=True)
class Action:
    kind: str        # "buy" | "sell"
    side: str
    count: float
    price: float     # limit price (the ask for buys, the bid for sells)
    reason: str = ""


def value(side: str, p: float) -> float:
    return p if side == "yes" else 1 - p


def net_if_sold(pos: Position, bid: float) -> float:
    return (bid - pos.price) * pos.count - pos.fee_paid - fee(pos.count, bid)


def decide(pos: Position | None, p: float, q: Quote, secs_left: float, equity: float,
           entries_so_far: int, params: Params = Params()) -> Action | None:
    if pos is not None:
        bid = q.bid(pos.side)
        if bid is None or bid <= 0 or secs_left < params.min_secs_exit:
            return None
        net = net_if_sold(pos, bid)
        if net >= params.take_profit * pos.count:
            return Action("sell", pos.side, pos.count, bid, "take_profit")
        if net > 0 and bid >= value(pos.side, p) + params.exit_margin:
            return Action("sell", pos.side, pos.count, bid, "above_fair")
        return None

    if secs_left < params.min_secs_enter or entries_so_far >= params.max_entries:
        return None
    best = None
    for side in ("yes", "no"):
        ask = q.ask(side)
        if ask is None or not (params.min_price <= ask <= params.max_price):
            continue
        edge = value(side, p) - ask - fee(1, ask)
        if edge >= params.entry_edge and (best is None or edge > best[1]):
            best = (side, edge, ask)
    if best is None:
        return None
    side, _, ask = best
    budget = params.frac * equity
    count = math.floor(budget / (ask + fee(1, ask)) * 100) / 100  # Kalshi allows 0.01-contract granularity
    if count < params.min_count or count * ask + fee(count, ask) > equity:
        return None
    return Action("buy", side, count, ask, "edge")

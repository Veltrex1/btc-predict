"""Real-time Polymarket US order books and trades from the authenticated Markets WebSocket.

    uv run python -m recorder.pmus_ws --check        # connect for ~25 s and print what arrives (read-only)

Why this exists: the public REST book (gateway.polymarket.us/v1/markets/{slug}/book) is served from Cloudflare's cache
(`cache-control: public, max-age=30`), so its prices can be 30 s old. Real-time prices are only on
wss://api.polymarket.us/v1/ws/markets, which needs the API key in the handshake:
  X-PM-Access-Key: key id,  X-PM-Timestamp: ms (within 30 s of server time),
  X-PM-Signature: base64(Ed25519 over timestamp + "GET" + "/v1/ws/markets").

The docs show two request styles (camelCase string enums, snake_case numeric enums); the feed tries camelCase first and
switches if the server answers with an error, then keeps whichever worked. Windows are listed just in time, so the
current window and (from 2 minutes before it opens) the next one are re-subscribed every second until the server accepts.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import websockets

WS_URL = "wss://api.polymarket.us/v1/ws/markets"
PATH = "/v1/ws/markets"
IDLE_S = 60            # no frame (not even a heartbeat) for this long: reconnect
RETRY_S = 5            # re-send a subscription that has produced nothing after this long
log = logging.getLogger("pmus_ws")


def pmus_slug(start: datetime) -> str:
    return start.strftime("cpc-btc-updown-15m-%Y-%m-%d-") + f"{start.hour:02d}{start.minute:02d}z"


def wanted_slugs(now: datetime, lookahead_s: float = 120) -> list[str]:
    """The window that is open now and, shortly before it opens, the next one."""
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    out = [pmus_slug(start)]
    if (start + timedelta(minutes=15) - now).total_seconds() < lookahead_s:
        out.append(pmus_slug(start + timedelta(minutes=15)))
    return out


def handshake_headers(key_id: str, key, now_ms: int | None = None) -> dict:
    ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
    sig = base64.b64encode(key.sign(f"{ts}GET{PATH}".encode())).decode()
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts, "X-PM-Signature": sig}


# ── messages ──────────────────────────────────────────────────────────
SUB_TYPES = {"book": ("SUBSCRIPTION_TYPE_MARKET_DATA", 1), "trade": ("SUBSCRIPTION_TYPE_TRADE", 3)}


def subscribe_msg(fmt: str, kind: str, slug: str, request_id: str) -> dict:
    name, number = SUB_TYPES[kind]
    if fmt == "camel":
        return {"subscribe": {"requestId": request_id, "subscriptionType": name, "marketSlugs": [slug]}}
    return {"subscribe": {"request_id": request_id, "subscription_type": number, "market_slugs": [slug]}}


def _get(d: dict, *names):
    """First present key among the camelCase / snake_case spellings."""
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return None


def _num(v) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ms(iso) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


@dataclass(frozen=True)
class Book:
    slug: str
    state: str | None
    bid: float | None
    bid_sz: float
    ask: float | None
    ask_sz: float
    exch_ts: int | None
    recv_ts: int

    def top(self) -> tuple:
        return (self.state, self.bid, self.bid_sz, self.ask, self.ask_sz)


@dataclass(frozen=True)
class Trade:
    slug: str
    price: float | None
    qty: float | None
    taker_intent: str | None
    trade_ts: int | None
    recv_ts: int


def _levels(raw) -> list[tuple[float, float]]:
    out = []
    for x in raw or []:
        px, qty = _num(_get(x, "px", "price")), _num(_get(x, "qty", "quantity"))
        if px is not None:
            out.append((px, qty or 0.0))
    return out


def parse_frame(msg: dict, recv_ts: int) -> list:
    """Normalise one WebSocket frame into Book / Trade objects, or ('error', request_id, text) / ('heartbeat',)."""
    if "heartbeat" in msg:
        return [("heartbeat",)]
    if msg.get("error"):
        return [("error", _get(msg, "requestId", "request_id"), str(msg["error"]))]
    out = []
    md = _get(msg, "marketData", "market_data")
    if md:
        bids = sorted(_levels(md.get("bids")), reverse=True)
        asks = sorted(_levels(md.get("offers")))
        out.append(Book(_get(md, "marketSlug", "market_slug") or "", md.get("state"),
                        bids[0][0] if bids else None, bids[0][1] if bids else 0.0,
                        asks[0][0] if asks else None, asks[0][1] if asks else 0.0,
                        _ms(_get(md, "transactTime", "transact_time")), recv_ts))
    lite = _get(msg, "marketDataLite", "market_data_lite")
    if lite:
        out.append(Book(_get(lite, "marketSlug", "market_slug") or "", None, _num(_get(lite, "bestBid", "best_bid")),
                        0.0, _num(_get(lite, "bestAsk", "best_ask")), 0.0, None, recv_ts))
    tr = msg.get("trade")
    if tr:
        out.append(Trade(_get(tr, "marketSlug", "market_slug") or "", _num(tr.get("price")),
                         _num(_get(tr, "quantity", "qty")), (tr.get("taker") or {}).get("intent"),
                         _ms(_get(tr, "tradeTime", "trade_time")), recv_ts))
    return out


# ── the feed ──────────────────────────────────────────────────────────
class PMBookFeed:
    """Keeps the latest top of book per slug. `on_book` fires only when the top of book changes."""

    def __init__(self, key_id: str, key, on_book: Callable[[Book], None] | None = None,
                 on_trade: Callable[[Trade], None] | None = None, url: str = WS_URL,
                 wanted: Callable[[], list[str]] | None = None, tick_s: float = 1.0,
                 raw_sink: Callable[[str], None] | None = None):
        self.key_id, self.key, self.url = key_id, key, url
        self.on_book, self.on_trade, self.raw_sink = on_book, on_trade, raw_sink
        self.wanted = wanted or (lambda: wanted_slugs(datetime.now(UTC)))
        self.tick_s = tick_s
        self.books: dict[str, Book] = {}
        self.fmt: str | None = None            # confirmed request style
        self._try = ["camel", "snake"]
        self._pending: dict[str, tuple[str, str, float]] = {}   # request_id -> (slug, fmt, sent_at)
        self._sent_fmt: dict[str, str] = {}    # request_id -> style it was sent in (kept, to attribute late errors)
        self._rotated_at = 0.0
        self._seen: set[str] = set()           # slugs that have produced data on this connection
        self.errors: list[str] = []
        self.frames = 0

    def _fmt(self) -> str:
        return self.fmt or self._try[0]

    async def run_once(self) -> None:
        """One connection, until it drops or goes silent (the caller reconnects with backoff)."""
        headers = handshake_headers(self.key_id, self.key)
        self._pending, self._seen, self._sent_fmt = {}, set(), {}
        async with websockets.connect(self.url, additional_headers=headers, ping_interval=20,
                                      max_size=2**22) as ws:
            sub = asyncio.create_task(self._subscriber(ws))
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=IDLE_S)
                    self.handle(raw if isinstance(raw, str) else raw.decode())
                    if sub.done():
                        sub.result()
            finally:
                sub.cancel()

    async def _subscriber(self, ws) -> None:
        while True:
            now = time.time()
            for slug in self.wanted():
                if slug in self._seen:
                    continue
                if any(s == slug and now - at < RETRY_S for s, _, at in self._pending.values()):
                    continue
                fmt = self._fmt()
                for kind in ("book", "trade"):
                    rid = f"{kind}-{slug}-{int(now * 1000)}"
                    self._pending[rid] = (slug, fmt, now)
                    self._sent_fmt[rid] = fmt
                    await ws.send(json.dumps(subscribe_msg(fmt, kind, slug, rid)))
            await asyncio.sleep(self.tick_s)

    def handle(self, raw: str) -> None:
        if self.raw_sink:
            self.raw_sink(raw)
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        self.frames += 1
        recv = int(time.time() * 1000)
        for ev in parse_frame(msg, recv):
            if isinstance(ev, tuple):
                if ev[0] == "error":
                    self._on_error(ev[1], ev[2])
                continue
            if isinstance(ev, Book):
                self._confirm(ev.slug)
                prev = self.books.get(ev.slug)
                self.books[ev.slug] = ev
                if (prev is None or prev.top() != ev.top()) and self.on_book:
                    self.on_book(ev)
            elif isinstance(ev, Trade):
                self._confirm(ev.slug)
                if self.on_trade:
                    self.on_trade(ev)

    def _confirm(self, slug: str) -> None:
        self._seen.add(slug)
        if self.fmt is None:
            fmts = [f for s, f, _ in self._pending.values() if s == slug]
            if fmts:
                self.fmt = fmts[0]
                log.info("Polymarket US WebSocket request style: %s", self.fmt)

    def _on_error(self, request_id: str | None, text: str) -> None:
        if "already subscribed" in text.lower():
            # Not a failure: the server has the subscription (a window listed ahead of its opening is quiet until it
            # opens). It also proves the request style was understood, so stop re-sending and keep the style.
            slug = self._pending.get(request_id or "", (None,))[0]
            if slug:
                self._confirm(slug)
            return
        self.errors.append(text)
        if len(self.errors) <= 3 or len(self.errors) % 60 == 0:  # an unlisted window errors every few seconds
            log.warning("Polymarket US WebSocket error for %s: %s (%d so far)", request_id, text[:200], len(self.errors))
        if self.fmt is None and not self._seen:
            # Nothing has worked yet: the request style may be wrong, so try the other one. Both subscriptions of a
            # tick get rejected, so flip only for an error about the style currently in use (and, when the error
            # names no request, at most once a second). If the error was only "market not listed yet" the flip is
            # harmless: the style that gets data flowing is the one that is kept.
            fmt = self._sent_fmt.get(request_id or "")
            now = time.time()
            if fmt == self._try[0] or (fmt is None and now - self._rotated_at > 1.0):
                self._try.append(self._try.pop(0))
                self._rotated_at = now
                self._pending = {}


async def check(seconds: float = 25) -> None:
    """Read-only: connect with the configured key, print raw frames and what we learned."""
    from server.polymarket_us_client import private_key_from_secret
    kid, sec = os.environ.get("POLYMARKET_KEY_ID"), os.environ.get("POLYMARKET_SECRET_KEY")
    if not (kid and sec):
        print("POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY are not set")
        return
    raws: list[str] = []
    books: list[Book] = []
    feed = PMBookFeed(kid.strip(), private_key_from_secret(sec), on_book=books.append,
                      raw_sink=lambda r: raws.append(r) if len(raws) < 6 else None)
    t0 = time.time()
    try:
        await asyncio.wait_for(feed.run_once(), timeout=seconds)
    except TimeoutError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"CONNECTION FAILED: {type(e).__name__}: {e}")
        if "403" in str(e) or "VPN" in str(e).upper():
            print("Polymarket refuses this machine's address for the data feed too.")
        return
    dt = max(time.time() - t0, 1e-9)
    for r in raws:
        print("frame:", r[:500])
    lags = [b.recv_ts - b.exch_ts for b in books if b.exch_ts]
    print(f"\nrequest style that worked: {feed.fmt or 'none confirmed'}; frames {feed.frames}; "
          f"book changes {len(books)} ({len(books) / dt:.2f}/s); errors {feed.errors[:3]}")
    if lags:
        print(f"receipt lag behind the exchange timestamp: median {statistics.median(lags)} ms, max {max(lags)} ms")
    for slug, b in feed.books.items():
        print(f"{slug}: bid {b.bid} x {b.bid_sz} / ask {b.ask} x {b.ask_sz} ({b.state})")
    if not feed.books:
        print("No book arrived. Run again in a minute (windows are listed just in time) and paste this output.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--seconds", type=float, default=25)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if a.check:
        asyncio.run(check(a.seconds))
    else:
        ap.print_help()

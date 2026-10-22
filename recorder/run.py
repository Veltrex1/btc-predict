"""High-resolution market recorder: the raw material for finding (and re-testing) edges.

    uv run python -m recorder.run            # runs forever; writes data/ticks.sqlite

Captures, with a local receive timestamp in milliseconds:
  * Kalshi KXBTC15M: order book (top 5 levels, both sides) every second for the open market,
    and every public trade (price, size, taker side).
  * BTC spot top-of-book + trades from the BRTI constituent venues reachable from the US:
    Coinbase, Kraken, Bitstamp, Gemini (websockets).
  * Deribit BTC-PERPETUAL ticker (mark, index, best bid/ask, funding) and DVOL implied-volatility index.
Binance and Bybit block US IPs, so they are not recorded.

Everything is append-only so later analysis (lead-lag, quote staleness, flow
imbalance, maker fill rates) can be replayed exactly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import httpx
import websockets

from pipeline.config import DATA_DIR, KALSHI_BASE, SERIES
from recorder.pmus_ws import PMBookFeed, pmus_slug  # noqa: F401  (pmus_slug is re-exported)

log = logging.getLogger("recorder")
DB = Path(DATA_DIR) / "ticks.sqlite"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS spot (ts INTEGER, venue TEXT, kind TEXT, bid REAL, ask REAL, price REAL, size REAL, side TEXT);
CREATE INDEX IF NOT EXISTS spot_ts ON spot(ts);
CREATE TABLE IF NOT EXISTS kalshi_book (ts INTEGER, ticker TEXT, yes_bid REAL, yes_bid_sz REAL, yes_ask REAL,
    yes_ask_sz REAL, yes_levels TEXT, no_levels TEXT);
CREATE INDEX IF NOT EXISTS kb_ts ON kalshi_book(ts);
CREATE TABLE IF NOT EXISTS kalshi_trades (trade_id TEXT PRIMARY KEY, ts INTEGER, ticker TEXT, yes_price REAL,
    count REAL, taker_side TEXT, recv_ts INTEGER);
CREATE TABLE IF NOT EXISTS kalshi_markets (ticker TEXT PRIMARY KEY, open_ts INTEGER, close_ts INTEGER, target REAL,
    result TEXT, expiration_value REAL);
CREATE TABLE IF NOT EXISTS brti (ts INTEGER, feed TEXT, source_ts INTEGER, value REAL, avg60 REAL,
    settle_avg REAL, settle_n INTEGER);
CREATE INDEX IF NOT EXISTS brti_ts ON brti(source_ts);
CREATE TABLE IF NOT EXISTS kalshi_ob (ts INTEGER, ticker TEXT, kind TEXT, side TEXT, price REAL, size REAL,
    levels TEXT, seq INTEGER, exch_ts INTEGER);
CREATE INDEX IF NOT EXISTS kob_ts ON kalshi_ob(ts);
CREATE TABLE IF NOT EXISTS pmus_book (ts INTEGER, slug TEXT, state TEXT, best_bid REAL, bid_sz REAL,
    best_ask REAL, ask_sz REAL, bids TEXT, offers TEXT, last_px REAL, notional REAL);
CREATE INDEX IF NOT EXISTS pmus_ts ON pmus_book(ts);
-- real-time Polymarket US books from the authenticated WebSocket (pmus_book above is the 30 s cached REST copy)
CREATE TABLE IF NOT EXISTS pmus_ws (ts INTEGER, slug TEXT, state TEXT, best_bid REAL, bid_sz REAL, best_ask REAL,
    ask_sz REAL, exch_ts INTEGER);
CREATE INDEX IF NOT EXISTS pmus_ws_ts ON pmus_ws(ts);
CREATE TABLE IF NOT EXISTS pmus_trades (ts INTEGER, slug TEXT, price REAL, qty REAL, taker_intent TEXT, trade_ts INTEGER);
CREATE INDEX IF NOT EXISTS pmus_trades_ts ON pmus_trades(ts);
CREATE TABLE IF NOT EXISTS pmus_results (slug TEXT PRIMARY KEY, close_ts INTEGER, state TEXT, settle_px REAL);
CREATE TABLE IF NOT EXISTS deribit (ts INTEGER, instrument TEXT, mark REAL, index_price REAL, bid REAL, ask REAL,
    funding REAL, open_interest REAL);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Sink:
    """Buffers rows and flushes them every couple of seconds (one writer, WAL mode)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=120)  # wait out compaction batches instead of failing
        self.db.executescript(SCHEMA)
        self.buf: dict[str, list[tuple]] = {}
        self.counts: dict[str, int] = {}

    def add(self, table: str, row: tuple) -> None:
        self.buf.setdefault(table, []).append(row)

    def flush(self) -> None:
        for table, rows in self.buf.items():
            if rows:
                ph = ",".join("?" * len(rows[0]))
                verb = "INSERT OR IGNORE" if table in ("kalshi_trades",) else \
                    "INSERT OR REPLACE" if table in ("kalshi_markets", "pmus_results") else "INSERT"
                self.db.executemany(f"{verb} INTO {table} VALUES ({ph})", rows)
                self.counts[table] = self.counts.get(table, 0) + len(rows)
        self.db.commit()
        self.buf = {}


SINK: Sink | None = None


async def keep(name: str, coro_fn) -> None:
    """Run a feed forever, reconnecting with backoff."""
    delay = 1
    while True:
        try:
            await coro_fn()
            delay = 1
        except Exception as e:  # noqa: BLE001
            log.warning("%s: %s; reconnect in %ss", name, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


# ── spot venues ───────────────────────────────────────────────────────
async def coinbase() -> None:
    async with websockets.connect("wss://ws-feed.exchange.coinbase.com", ping_interval=20) as ws:
        await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "ticker":
                SINK.add("spot", (now_ms(), "coinbase", "tick", float(m["best_bid"]), float(m["best_ask"]),
                                  float(m["price"]), float(m.get("last_size") or 0), m.get("side")))


async def kraken() -> None:
    async with websockets.connect("wss://ws.kraken.com/v2", ping_interval=20) as ws:
        await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"],
                                                                     "event_trigger": "bbo"}}))
        await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}}))
        async for raw in ws:
            m = json.loads(raw)
            ch, data = m.get("channel"), m.get("data") or []
            if ch == "ticker":
                for d in data:
                    SINK.add("spot", (now_ms(), "kraken", "bbo", d["bid"], d["ask"], d.get("last"), None, None))
            elif ch == "trade":
                for d in data:
                    SINK.add("spot", (now_ms(), "kraken", "trade", None, None, d["price"], d["qty"], d["side"]))


async def bitstamp() -> None:
    async with websockets.connect("wss://ws.bitstamp.net", ping_interval=20) as ws:
        for ch in ("live_trades_btcusd", "order_book_btcusd"):
            await ws.send(json.dumps({"event": "bts:subscribe", "data": {"channel": ch}}))
        async for raw in ws:
            m = json.loads(raw)
            d = m.get("data") or {}
            if m.get("event") == "trade":
                SINK.add("spot", (now_ms(), "bitstamp", "trade", None, None, float(d["price"]), float(d["amount"]),
                                  "buy" if d.get("type") == 0 else "sell"))
            elif m.get("event") == "data" and d.get("bids"):
                SINK.add("spot", (now_ms(), "bitstamp", "bbo", float(d["bids"][0][0]), float(d["asks"][0][0]),
                                  None, None, None))


async def gemini() -> None:
    url = "wss://api.gemini.com/v1/marketdata/BTCUSD?top_of_book=true&trades=true"
    bid = ask = None
    async with websockets.connect(url, ping_interval=20) as ws:
        async for raw in ws:
            m = json.loads(raw)
            for e in m.get("events", []):
                if e.get("type") == "change":
                    if e["side"] == "bid":
                        bid = float(e["price"])
                    else:
                        ask = float(e["price"])
                    SINK.add("spot", (now_ms(), "gemini", "bbo", bid, ask, None, None, None))
                elif e.get("type") == "trade":
                    SINK.add("spot", (now_ms(), "gemini", "trade", None, None, float(e["price"]), float(e["amount"]),
                                      "buy" if e.get("makerSide") == "ask" else "sell"))


async def deribit() -> None:
    async with websockets.connect("wss://www.deribit.com/ws/api/v2", ping_interval=20) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/subscribe", "params": {
            "channels": ["ticker.BTC-PERPETUAL.100ms", "deribit_volatility_index.btc_usd"]}}))
        async for raw in ws:
            m = json.loads(raw)
            p = (m.get("params") or {})
            ch, d = p.get("channel", ""), p.get("data") or {}
            if ch.startswith("ticker."):
                SINK.add("deribit", (now_ms(), "BTC-PERPETUAL", d.get("mark_price"), d.get("index_price"),
                                     d.get("best_bid_price"), d.get("best_ask_price"), d.get("current_funding"),
                                     d.get("open_interest")))
            elif ch.startswith("deribit_volatility_index"):
                SINK.add("deribit", (now_ms(), "DVOL", d.get("volatility"), None, None, None, None, None))


# ── Kalshi ────────────────────────────────────────────────────────────
async def kalshi() -> None:
    """Order book every second and trades every 2 seconds for the open 15-minute market (public REST)."""
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "btc-predict-recorder"}) as c:
        market, last_trade_poll, cursor_ts = None, 0.0, 0
        while True:
            t0 = time.time()
            if market is None or t0 * 1000 >= market["close_ts"]:
                r = await c.get(f"{KALSHI_BASE}/markets", params={"series_ticker": SERIES, "status": "open"})
                ms = sorted(r.json().get("markets", []), key=lambda m: m["close_time"])
                ms = [m for m in ms if _ms(m["close_time"]) > now_ms()]
                if ms:
                    m = ms[0]
                    market = {"ticker": m["ticker"], "close_ts": _ms(m["close_time"]), "open_ts": _ms(m["open_time"])}
                    SINK.add("kalshi_markets", (m["ticker"], market["open_ts"], market["close_ts"],
                                                m.get("floor_strike"), None, None))
                    cursor_ts = market["open_ts"] // 1000
            if market:
                r = await c.get(f"{KALSHI_BASE}/markets/{market['ticker']}/orderbook", params={"depth": 5})
                ob = r.json().get("orderbook_fp", {})
                yes = [(float(p), float(q)) for p, q in ob.get("yes_dollars") or []]
                no = [(float(p), float(q)) for p, q in ob.get("no_dollars") or []]
                by, bn = (max(yes) if yes else (None, 0)), (max(no) if no else (None, 0))
                SINK.add("kalshi_book", (now_ms(), market["ticker"], by[0], by[1],
                                         round(1 - bn[0], 4) if bn[0] is not None else None, bn[1],
                                         json.dumps(yes), json.dumps(no)))
                if t0 - last_trade_poll >= 2:
                    last_trade_poll = t0
                    r = await c.get(f"{KALSHI_BASE}/markets/trades",
                                    params={"ticker": market["ticker"], "min_ts": cursor_ts, "limit": 1000})
                    for tr in r.json().get("trades", []):
                        yp = tr.get("yes_price_dollars") or tr.get("yes_price")
                        yp = float(yp) if isinstance(yp, str) else (yp or 0) / 100
                        SINK.add("kalshi_trades", (tr["trade_id"], _ms(tr["created_time"]), tr["ticker"], yp,
                                                   float(tr.get("count_fp") or tr.get("count") or 0),
                                                   tr.get("taker_outcome_side") or tr.get("taker_side"), now_ms()))
            await asyncio.sleep(max(0.0, 1.0 - (time.time() - t0)))


# ── Kalshi authenticated websocket (needs KALSHI_KEY_ID + KALSHI_PRIVATE_KEY_PATH) ──────
KALSHI_WS = os.environ.get("KALSHI_WS", "wss://api.elections.kalshi.com/trade-api/ws/v2")


def parse_cf(msg: dict, feed: str) -> tuple:
    """One row for the brti table from a cfbenchmarks_value(_5hz) message body."""
    if feed == "5hz":
        return (now_ms(), feed, msg.get("source_ts_ms"), float(msg["value_usd"]), None, None, None)
    raw = json.loads(msg.get("data") or "{}")
    a60 = msg.get("avg_60s_data") or {}
    s15 = msg.get("last_60s_windowed_average_15min") or {}
    return (now_ms(), feed, raw.get("time"), float(raw["value"]) if raw.get("value") else None,
            float(a60["value"]) if a60.get("value") else None,
            float(s15["value"]) if s15.get("value") else None, s15.get("window_size"))


async def kalshi_ws() -> None:
    from server.kalshi_client import load_private_key, sign
    key = load_private_key(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    ts = str(now_ms())
    headers = {"KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"], "KALSHI-ACCESS-TIMESTAMP": ts,
               "KALSHI-ACCESS-SIGNATURE": sign(key, ts + "GET" + "/trade-api/ws/v2")}
    async with websockets.connect(KALSHI_WS, additional_headers=headers, ping_interval=20, max_size=2**23) as ws:
        cmd_id = 1

        async def send(params: dict) -> None:
            nonlocal cmd_id
            await ws.send(json.dumps({"id": cmd_id, "cmd": "subscribe", "params": params}))
            cmd_id += 1

        await send({"channels": ["cfbenchmarks_value"], "index_ids": ["BRTI"]})
        await send({"channels": ["cfbenchmarks_value_5hz"], "index_ids": ["BRTI"]})
        watched: set[str] = set()

        async def watch_markets() -> None:
            async with httpx.AsyncClient(timeout=10) as c:
                while True:
                    r = await c.get(f"{KALSHI_BASE}/markets", params={"series_ticker": SERIES, "status": "open"})
                    for m in r.json().get("markets", []):
                        if m["ticker"] not in watched and _ms(m["close_time"]) - now_ms() < 20 * 60_000:
                            watched.add(m["ticker"])
                            await send({"channels": ["orderbook_delta", "trade"], "market_tickers": [m["ticker"]],
                                        "use_yes_price": True})
                    await asyncio.sleep(20)

        watcher = asyncio.create_task(watch_markets())
        try:
            async for raw in ws:
                m = json.loads(raw)
                typ, body = m.get("type"), m.get("msg") or {}
                if typ == "cfbenchmarks_value" and body.get("index_id") == "BRTI":
                    SINK.add("brti", parse_cf(body, "1hz"))
                elif typ == "cfbenchmarks_value_5hz" and body.get("index_id") == "BRTI":
                    SINK.add("brti", parse_cf(body, "5hz"))
                elif typ == "orderbook_snapshot":
                    SINK.add("kalshi_ob", (now_ms(), body.get("market_ticker"), "snapshot", None, None, None,
                                           json.dumps({"yes": body.get("yes_dollars_fp"), "no": body.get("no_dollars_fp")}),
                                           m.get("seq"), None))
                elif typ == "orderbook_delta":
                    SINK.add("kalshi_ob", (now_ms(), body.get("market_ticker"), "delta", body.get("side"),
                                           float(body["price_dollars"]), float(body["delta_fp"]), None, m.get("seq"),
                                           body.get("ts_ms")))
                elif typ == "trade":
                    t_ms = int(body.get("ts_ms") or int(body.get("ts", 0)) * 1000)
                    SINK.add("kalshi_trades", (body["trade_id"], t_ms, body.get("market_ticker"),
                                               float(body["yes_price_dollars"]), float(body["count_fp"]),
                                               body.get("taker_outcome_side") or body.get("taker_side"), now_ms()))
                elif typ == "error":
                    log.warning("kalshi ws error: %s", body)
        finally:
            watcher.cancel()


# ── Polymarket US (public order books; same BRTI 60 s settlement as Kalshi) ─────────
PMUS = "https://gateway.polymarket.us/v1/markets"


def parse_pmus(md: dict) -> tuple:
    def lv(side):
        return [(float(x["px"]["value"]), float(x["qty"])) for x in (md.get(side) or [])]
    bids = sorted(lv("bids"), reverse=True)
    offers = sorted(lv("offers"))
    st = md.get("stats") or {}
    last = (st.get("lastTradePx") or {}).get("value")
    notional = (st.get("notionalTraded") or {}).get("value")
    return (now_ms(), md.get("marketSlug"), md.get("state"),
            bids[0][0] if bids else None, bids[0][1] if bids else None,
            offers[0][0] if offers else None, offers[0][1] if offers else None,
            json.dumps(bids[:5]), json.dumps(offers[:5]),
            float(last) if last else None, float(notional) if notional else None)


async def polymarket_us() -> None:
    """Every second: order book of the current 15-minute BTC window and, near the boundary, the next one."""
    from datetime import datetime, timedelta, timezone
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "btc-predict-recorder"}) as c:
        while True:
            t0 = time.time()
            now = datetime.now(timezone.utc)
            start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
            slugs = [pmus_slug(start)]
            if (start + timedelta(minutes=15) - now).total_seconds() < 120:
                slugs.append(pmus_slug(start + timedelta(minutes=15)))
            for slug in slugs:
                r = await c.get(f"{PMUS}/{slug}/book")
                if r.status_code == 200 and (md := r.json().get("marketData")):
                    SINK.add("pmus_book", parse_pmus(md))
            await asyncio.sleep(max(0.0, 1.0 - (time.time() - t0)))


def pmus_settle(md: dict) -> float | None:
    """Settlement price of the Up outcome (1.0 = Up won) from an expired Polymarket US book, else None."""
    v = ((md.get("stats") or {}).get("settlementPx") or {}).get("value")
    return float(v) if v is not None and md.get("state") == "MARKET_STATE_EXPIRED" else None


async def polymarket_ws() -> None:
    """Real-time Polymarket US books (changes only) and trades from the authenticated Markets WebSocket."""
    from server.polymarket_us_client import private_key_from_secret
    feed = PMBookFeed(
        os.environ["POLYMARKET_KEY_ID"].strip(), private_key_from_secret(os.environ["POLYMARKET_SECRET_KEY"]),
        on_book=lambda b: SINK.add("pmus_ws", (b.recv_ts, b.slug, b.state, b.bid, b.bid_sz, b.ask, b.ask_sz, b.exch_ts)),
        on_trade=lambda t: SINK.add("pmus_trades", (t.recv_ts, t.slug, t.price, t.qty, t.taker_intent, t.trade_ts)))
    await feed.run_once()


async def pmus_results() -> None:
    """Fill in Polymarket US settlements for windows that closed in the last two days."""
    from datetime import datetime, timezone
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "btc-predict-recorder"}) as c:
        while True:
            rows = SINK.db.execute(
                "SELECT close_ts FROM kalshi_markets WHERE close_ts < ? AND close_ts > ? AND "
                "close_ts NOT IN (SELECT close_ts FROM pmus_results WHERE settle_px IS NOT NULL) ORDER BY close_ts DESC",
                (now_ms() - 60_000, now_ms() - 2 * 86_400_000)).fetchall()
            for (close_ts,) in rows[:20]:
                slug = pmus_slug(datetime.fromtimestamp(close_ts / 1000 - 900, tz=timezone.utc))
                r = await c.get(f"{PMUS}/{slug}/book")
                if r.status_code == 200 and (md := r.json().get("marketData")) and (px := pmus_settle(md)) is not None:
                    SINK.add("pmus_results", (slug, close_ts, md.get("state"), px))
            await asyncio.sleep(60)


async def kalshi_results() -> None:
    """Fill in official results for recorded markets once they settle."""
    async with httpx.AsyncClient(timeout=10) as c:
        while True:
            rows = SINK.db.execute("SELECT ticker FROM kalshi_markets WHERE result IS NULL AND close_ts < ?",
                                   (now_ms() - 30_000,)).fetchall()
            for (ticker,) in rows[:20]:
                r = await c.get(f"{KALSHI_BASE}/markets/{ticker}")
                m = r.json().get("market", {})
                if m.get("result") in ("yes", "no"):
                    SINK.add("kalshi_markets", (ticker, _ms(m["open_time"]), _ms(m["close_time"]),
                                                m.get("floor_strike"), m["result"],
                                                float(m["expiration_value"]) if m.get("expiration_value") else None))
            await asyncio.sleep(30)


def _ms(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


async def flusher() -> None:
    last_report = time.time()
    while True:
        await asyncio.sleep(2)
        SINK.flush()
        if time.time() - last_report > 300:
            last_report = time.time()
            log.info("rows so far: %s", SINK.counts)


async def main() -> None:
    global SINK
    SINK = Sink(DB)
    feeds = {"coinbase": coinbase, "kraken": kraken, "bitstamp": bitstamp, "gemini": gemini,
             "deribit": deribit, "kalshi": kalshi, "kalshi_results": kalshi_results,
             "polymarket_us": polymarket_us, "pmus_results": pmus_results}
    if os.environ.get("PMUS_REST_POLL", "1") == "0":
        feeds.pop("polymarket_us")  # the cached REST book is only kept to measure how stale it is
    if os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY"):
        feeds["polymarket_ws"] = polymarket_ws
    else:
        log.info("no Polymarket US key configured: real-time Polymarket books disabled")
    if os.environ.get("KALSHI_KEY_ID") and os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        feeds["kalshi_ws"] = kalshi_ws
    else:
        log.info("no Kalshi API key configured: BRTI settlement stream and websocket order book disabled")
    await asyncio.gather(flusher(), *(keep(n, f) for n, f in feeds.items()))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())

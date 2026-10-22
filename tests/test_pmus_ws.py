import asyncio
import base64
import functools
import json

import pandas as pd
import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519

from pipeline import pmus_staleness, ticks
from recorder import pmus_ws as pw
from recorder.run import SCHEMA

SLUG = "cpc-btc-updown-15m-2026-10-22-1515z"


def md(bid, ask, bid_sz="30", ask_sz="20", state="MARKET_STATE_OPEN", camel=True):
    book = {"marketSlug": SLUG, "bids": [{"px": {"value": str(bid), "currency": "USD"}, "qty": bid_sz},
                                         {"px": {"value": str(bid - 0.01), "currency": "USD"}, "qty": "5"}],
            "offers": [{"px": {"value": str(ask + 0.01), "currency": "USD"}, "qty": "5"},
                       {"px": {"value": str(ask), "currency": "USD"}, "qty": ask_sz}],
            "state": state, "transactTime": "2026-10-22T15:21:28.500Z"}
    if camel:
        return {"requestId": "r", "marketData": book}
    book = {"market_slug": book["marketSlug"], "bids": book["bids"], "offers": book["offers"], "state": state,
            "transact_time": book["transactTime"]}
    return {"request_id": "r", "market_data": book}


def test_parse_frame_best_levels_in_both_casings():
    for camel in (True, False):
        (b,) = pw.parse_frame(md(0.86, 0.87, camel=camel), 1_000)
        assert (b.slug, b.bid, b.bid_sz, b.ask, b.ask_sz, b.state) == (SLUG, 0.86, 30.0, 0.87, 20.0, "MARKET_STATE_OPEN")
        assert b.exch_ts == 1_792_682_488_500 and b.recv_ts == 1_000


def test_parse_trade_error_and_heartbeat():
    (t,) = pw.parse_frame({"trade": {"marketSlug": SLUG, "price": {"value": "0.55"}, "quantity": {"value": "0.5"},
                                     "tradeTime": "2026-10-22T15:21:28Z", "taker": {"intent": "ORDER_INTENT_BUY_LONG"}}}, 5)
    assert (t.price, t.qty, t.taker_intent) == (0.55, 0.5, "ORDER_INTENT_BUY_LONG")
    assert pw.parse_frame({"request_id": "x", "error": "boom"}, 1) == [("error", "x", "boom")]
    assert pw.parse_frame({"heartbeat": {}}, 1) == [("heartbeat",)]
    assert pw.parse_frame({"marketData": {"marketSlug": SLUG, "bids": [], "offers": []}}, 1)[0].bid is None


def test_wanted_slugs_adds_the_next_window_two_minutes_before_it_opens():
    from datetime import UTC, datetime
    assert pw.wanted_slugs(datetime(2026, 10, 22, 15, 21, 0, tzinfo=UTC)) == [SLUG]
    assert pw.wanted_slugs(datetime(2026, 10, 22, 15, 28, 30, tzinfo=UTC)) == [SLUG, SLUG.replace("1515z", "1530z")]


def test_book_callback_fires_only_when_the_top_of_book_changes():
    seen = []
    feed = pw.PMBookFeed("k", None, on_book=seen.append)
    for ask in (0.87, 0.87, 0.88):
        feed.handle(json.dumps(md(0.86, ask)))
    assert [b.ask for b in seen] == [0.87, 0.88] and feed.books[SLUG].ask == 0.88


# ── against a local WebSocket server ─────────────────────────────────
KEY = ed25519.Ed25519PrivateKey.generate()


def run_async(fn):
    """Run an async test body with asyncio.run (no pytest-asyncio dependency)."""
    @functools.wraps(fn)
    def wrapper():
        asyncio.run(fn())
    return wrapper


async def serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}{pw.PATH}"


def feed_for(url, **kw):
    return pw.PMBookFeed("key-id", KEY, url=url, wanted=lambda: [SLUG], tick_s=0.02, **kw)


@run_async
async def test_handshake_is_signed_and_subscriptions_follow_the_working_style():
    seen = {}

    async def handler(ws):
        seen["headers"] = ws.request.headers
        msgs = []
        while len(msgs) < 2:
            msgs.append(json.loads(await ws.recv()))
        seen["subs"] = msgs
        await ws.send(json.dumps(md(0.86, 0.87)))
        await ws.send(json.dumps({"trade": {"marketSlug": SLUG, "price": {"value": "0.86"}, "quantity": {"value": "1"}}}))
        await asyncio.sleep(0.2)

    server, url = await serve(handler)
    books, trades = [], []
    feed = feed_for(url, on_book=books.append, on_trade=trades.append)
    try:
        await asyncio.wait_for(feed.run_once(), timeout=1.0)
    except (TimeoutError, websockets.ConnectionClosed):
        pass
    finally:
        server.close()
    h = seen["headers"]
    ts, sig = h["X-PM-Timestamp"], base64.b64decode(h["X-PM-Signature"])
    KEY.public_key().verify(sig, f"{ts}GET/v1/ws/markets".encode())   # raises if the signature is wrong
    assert h["X-PM-Access-Key"] == "key-id"
    kinds = {m["subscribe"]["subscriptionType"] for m in seen["subs"]}
    assert kinds == {"SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE"}
    assert books and books[0].bid == 0.86 and trades and trades[0].price == 0.86 and feed.fmt == "camel"


@run_async
async def test_switches_request_style_when_the_server_rejects_the_first():
    styles = []

    async def handler(ws):
        async for raw in ws:
            sub = json.loads(raw)["subscribe"]
            if "subscriptionType" in sub:      # this server only understands the snake_case numeric style
                styles.append("camel")
                await ws.send(json.dumps({"requestId": sub["requestId"], "error": "unknown field"}))
            else:
                styles.append("snake")
                await ws.send(json.dumps(md(0.5, 0.52, camel=False)))
                await asyncio.sleep(0.3)
                return

    server, url = await serve(handler)
    feed = feed_for(url)
    try:
        await asyncio.wait_for(feed.run_once(), timeout=2.0)
    except (TimeoutError, websockets.ConnectionClosed):
        pass
    finally:
        server.close()
    assert styles[0] == "camel" and "snake" in styles
    assert feed.fmt == "snake" and feed.books[SLUG].bid == 0.5


@run_async
async def test_reconnect_signs_a_new_handshake_and_resubscribes():
    connections = []

    async def handler(ws):
        connections.append(ws.request.headers["X-PM-Timestamp"])
        await ws.recv()
        await ws.send(json.dumps(md(0.5, 0.52)))
        await ws.close()      # drop the connection right after the first book

    server, url = await serve(handler)
    feed = feed_for(url)
    try:
        for _ in range(2):
            try:
                await asyncio.wait_for(feed.run_once(), timeout=1.0)
            except (TimeoutError, websockets.ConnectionClosed):
                pass
            await asyncio.sleep(0.01)
    finally:
        server.close()
    assert len(connections) == 2 and feed.books[SLUG].bid == 0.5


# ── data side ────────────────────────────────────────────────────────
def test_recorder_schema_and_reader_for_the_real_time_table(tmp_path):
    import sqlite3
    db = tmp_path / "t.sqlite"
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO pmus_ws VALUES (1000, ?, 'MARKET_STATE_OPEN', 0.86, 30, 0.87, 20, 900)", (SLUG,))
    c.commit()
    c.close()
    df = ticks.pmus_live(ticks.connect(db), 0, 2000)
    assert list(df.columns) == ["ts", "slug", "state", "best_bid", "bid_sz", "best_ask", "ask_sz"] and len(df) == 1
    assert ticks.pmus_live(ticks.connect(db), 2000, 3000).empty


def test_staleness_report_measures_how_far_the_cached_book_lags():
    live = pd.DataFrame([{"ts": 1_000, "slug": SLUG, "state": "MARKET_STATE_OPEN", "best_bid": 0.80, "best_ask": 0.82},
                         {"ts": 20_000, "slug": SLUG, "state": "MARKET_STATE_OPEN", "best_bid": 0.70, "best_ask": 0.72}])
    rest = pd.DataFrame([{"ts": 5_000, "slug": SLUG, "best_bid": 0.80, "best_ask": 0.82},     # matches real time
                         {"ts": 25_000, "slug": SLUG, "best_bid": 0.80, "best_ask": 0.82}])   # cache still shows old prices
    r = pmus_staleness.compare(rest, live)
    assert r["snapshots_compared"] == 2 and r["share_identical_to_real_time"] == 0.5
    assert r["mean_abs_mid_difference_cents"] == pytest.approx(5.0) and r["share_off_by_5c_or_more"] == 0.5
    assert "need both" in pmus_staleness.compare(rest, live.iloc[0:0])["status"]


def test_loop_arb_row_waits_for_real_time_data_instead_of_scoring_stale_prices(tmp_path, monkeypatch):
    from pipeline import arb, loop
    (tmp_path / "t.sqlite").touch()
    monkeypatch.setattr(arb, "DB", tmp_path / "t.sqlite")
    monkeypatch.setattr(arb, "panel_all_days", lambda *a, **k: pd.DataFrame())
    (row,) = loop.cand_arb()
    assert "waiting for real-time Polymarket data" in row["candidate"] and row["windows"] == 0 and not row["promote"]


def test_already_subscribed_reply_is_a_confirmation_not_an_error():
    feed = pw.PMBookFeed("k", None)
    feed._pending["book-x"] = ("cpc-next", "camel", 0.0)
    feed._on_error("book-x", "slug already subscribed: cpc-next")
    assert "cpc-next" in feed._seen and feed.fmt == "camel" and feed.errors == []
    assert feed._try[0] == "camel"          # no style flip


def test_a_style_that_is_rejected_is_flipped_exactly_once():
    feed = pw.PMBookFeed("k", None)
    for rid in ("book-a", "trade-a"):       # both subscriptions of a tick are rejected
        feed._sent_fmt[rid] = "camel"
        feed._on_error(rid, "unknown field")
    assert feed._try[0] == "snake" and len(feed.errors) == 2


def test_retention_for_the_busy_real_time_tables_is_two_weeks():
    from recorder import compact
    assert compact.KEEP["pmus_ws"][0] == compact.KEEP["pmus_trades"][0] == 14 * compact.DAY_MS

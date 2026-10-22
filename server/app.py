"""Live signal server for the Kalshi 15-minute BTC market.

    uv run uvicorn server.app:app --host 127.0.0.1 --port 8765

- Streams BTC-USD from Coinbase (websocket) and builds 1-minute bars.
- Polls Kalshi every 2s for the open KXBTC15M market (target, YES/NO quotes).
- Every second, computes P(settles above target) and applies the lock policy:
  the first moment p >= tau (ABOVE) or p <= 1 - tau (BELOW) with at least
  `min_lead_s` seconds left, the call locks and never changes.
- Logs every window, its locked call and Kalshi's official result to SQLite
  (data/live.sqlite). That log is the graded live month.

Endpoints: /signal (JSON), /swiftbar (menu-bar text), /history, /stats,
/series (current window, for the dashboard), / (dashboard, if built).
"""
from __future__ import annotations

import asyncio
import hmac
import os
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
import websockets
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from pipeline.config import COINBASE_REST, COINBASE_WS, KALSHI_BASE, LIVE_DB_PATH, MODEL_PATH, ROOT, SERIES, WINDOW_MIN
from pipeline.features import live_row
from pipeline.kalshi import fee_per_contract, market_target, parse_market, quote
from pipeline.labels import reference_price
from pipeline.model import Predictor

log = logging.getLogger("btc-signal")
ET = ZoneInfo("America/New_York")
WIN = pd.Timedelta(minutes=WINDOW_MIN)
MIN = pd.Timedelta(minutes=1)

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    window_open TEXT PRIMARY KEY,      -- ISO UTC
    ticker TEXT,
    target REAL,
    target_source TEXT,                -- kalshi | proxy
    first_seen_secs_left REAL,
    locked_side TEXT,                  -- ABOVE | BELOW | NULL (no call)
    locked_prob REAL,
    locked_at TEXT,
    locked_secs_left REAL,
    locked_price REAL,
    yes_ask_at_lock REAL,
    no_ask_at_lock REAL,
    result TEXT,                       -- yes | no (Kalshi official)
    settle_value REAL,
    model_kind TEXT,
    tau REAL,
    min_lead_s REAL
);
CREATE TABLE IF NOT EXISTS snapshots (
    window_open TEXT, ts TEXT, secs_left REAL, price REAL, prob REAL, yes_bid REAL, yes_ask REAL,
    PRIMARY KEY (window_open, ts)
);
"""


def utcnow() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)


class State:
    def __init__(self) -> None:
        self.model = Predictor.load(MODEL_PATH)
        self.bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "taker_buy"], dtype=float)
        self.cur_bar: dict | None = None
        self.price: float | None = None
        self.price_ts: float = 0.0
        self.market: dict | None = None
        self.market_ts: float = 0.0
        self.signal: dict = {"status": "starting"}
        self.db = self._open_db(LIVE_DB_PATH)
        self.last_snapshot: float = 0.0

    @staticmethod
    def _open_db(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.executescript(LIVE_SCHEMA)
        return con

    # ── price bars ────────────────────────────────────────────────────
    def on_trade(self, price: float, size: float, ts: pd.Timestamp) -> None:
        minute = ts.floor("1min")
        b = self.cur_bar
        if b is None or minute > b["ts"]:
            if b is not None:
                self.bars.loc[b["ts"]] = [b["open"], b["high"], b["low"], b["close"], b["volume"], np.nan]
                self.bars = self.bars.tail(3000)
            self.cur_bar = {"ts": minute, "open": price, "high": price, "low": price, "close": price, "volume": size}
        elif minute == b["ts"]:
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            b["volume"] += size
        self.price = price
        self.price_ts = time.time()

    def merge_rest_bars(self, df: pd.DataFrame) -> None:
        cur = self.cur_bar["ts"] if self.cur_bar else None
        if cur is not None:
            df = df[df.index < cur]
        merged = pd.concat([self.bars[~self.bars.index.isin(df.index)], df]).sort_index()
        self.bars = merged.tail(3000)


S: State | None = None


async def fetch_rest_bars(client: httpx.AsyncClient, minutes: int = 1700) -> pd.DataFrame:
    end = utcnow().floor("1min")
    frames = []
    t = end - pd.Timedelta(minutes=minutes)
    while t < end:
        t2 = min(t + pd.Timedelta(minutes=300), end)
        r = await client.get(f"{COINBASE_REST}/products/BTC-USD/candles",
                             params={"granularity": 60, "start": t.isoformat() + "Z",
                                     "end": (t2 - MIN).isoformat() + "Z"})
        r.raise_for_status()
        rows = r.json()
        if rows:
            df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
            df.index = pd.to_datetime(df["time"], unit="s")
            frames.append(df[["open", "high", "low", "close", "volume"]].astype(float))
        t = t2
        await asyncio.sleep(0.15)
    out = pd.concat(frames).sort_index() if frames else pd.DataFrame()
    out = out[~out.index.duplicated(keep="last")]
    out["taker_buy"] = np.nan
    return out


async def rest_loop() -> None:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "btc-predict"}) as client:
        minutes = 1700
        while True:
            try:
                S.merge_rest_bars(await fetch_rest_bars(client, minutes))
                if S.price is None and len(S.bars):
                    S.price = float(S.bars["close"].iloc[-1])
                    S.price_ts = time.time()
                minutes = 30  # afterwards, just patch recent gaps
            except Exception as e:  # noqa: BLE001
                log.warning("coinbase REST: %s", e)
            await asyncio.sleep(120)


async def ws_loop() -> None:
    sub = {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker", "heartbeat"]}
    while True:
        try:
            async with websockets.connect(COINBASE_WS, ping_interval=20, max_size=2**22) as ws:
                await ws.send(json.dumps(sub))
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "ticker" and m.get("price"):
                        ts = pd.Timestamp(m.get("time") or datetime.now(timezone.utc)).tz_convert(None)
                        S.on_trade(float(m["price"]), float(m.get("last_size") or 0), ts)
        except Exception as e:  # noqa: BLE001
            log.warning("coinbase WS: %s; reconnecting", e)
            await asyncio.sleep(3)


async def kalshi_loop() -> None:
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "btc-predict"}) as client:
        last_settle_check = 0.0
        while True:
            try:
                r = await client.get(f"{KALSHI_BASE}/markets", params={"series_ticker": SERIES, "status": "open",
                                                                       "limit": 50})
                r.raise_for_status()
                now = utcnow()
                best = None
                for m in r.json().get("markets", []):
                    p = parse_market(m)
                    if p["close_time"] is None or p["close_time"] <= now:
                        continue
                    if p["open_time"] is not None and p["open_time"] > now:
                        continue
                    if best is None or p["close_time"] < best["close_time"]:
                        best = {**p, **quote(m), "target": market_target(m)}
                if best:
                    S.market, S.market_ts = best, time.time()
                if time.time() - last_settle_check > 20:
                    last_settle_check = time.time()
                    await settle_pending(client)
            except Exception as e:  # noqa: BLE001
                log.warning("kalshi: %s", e)
            await asyncio.sleep(2)


async def settle_pending(client: httpx.AsyncClient) -> None:
    cutoff = (utcnow() - WIN - pd.Timedelta(seconds=30)).isoformat()
    rows = S.db.execute("SELECT window_open, ticker FROM windows WHERE result IS NULL AND ticker IS NOT NULL "
                        "AND window_open <= ? ORDER BY window_open DESC LIMIT 20", (cutoff,)).fetchall()
    for row in rows:
        r = await client.get(f"{KALSHI_BASE}/markets/{row['ticker']}")
        if r.status_code != 200:
            continue
        p = parse_market(r.json().get("market", {}))
        if p["result"]:
            S.db.execute("UPDATE windows SET result = ?, settle_value = ? WHERE window_open = ?",
                         (p["result"], p["expiration_value"], row["window_open"]))
            S.db.commit()


def proxy_target(window_open: pd.Timestamp) -> float | None:
    bar = window_open - MIN
    if bar in S.bars.index:
        return float(reference_price(S.bars.loc[[bar]], S.model.settle_mode).iloc[0])
    return None


def compute_signal() -> dict:
    now = utcnow()
    window_open = now.floor(f"{WINDOW_MIN}min")
    secs_left = (window_open + WIN - now).total_seconds()
    if S.price is None or time.time() - S.price_ts > 30 or len(S.bars) < 300:
        return {"status": "no_data", "secs_left": secs_left, "window_open": window_open.isoformat()}

    m = S.market if S.market and S.market["close_time"] == window_open + WIN else None
    target, target_source = (m["target"], "kalshi") if m and m.get("target") else (proxy_target(window_open), "proxy")
    if target is None:
        return {"status": "no_target", "secs_left": secs_left, "window_open": window_open.isoformat()}

    row = live_row(S.bars, now, S.price, window_open, target, S.model.settle_mode)
    p = float(S.model.predict(row)[0])
    pol = S.model.policy
    key = window_open.isoformat()

    rec = S.db.execute("SELECT * FROM windows WHERE window_open = ?", (key,)).fetchone()
    if rec is None:
        S.db.execute("INSERT INTO windows (window_open, ticker, target, target_source, first_seen_secs_left, "
                     "model_kind, tau, min_lead_s) VALUES (?,?,?,?,?,?,?,?)",
                     (key, m["ticker"] if m else None, target, target_source, secs_left,
                      S.model.kind, pol.tau, pol.min_lead_s))
        S.db.commit()
        rec = S.db.execute("SELECT * FROM windows WHERE window_open = ?", (key,)).fetchone()
    elif m and (rec["ticker"] is None or rec["target_source"] != "kalshi"):
        S.db.execute("UPDATE windows SET ticker = ?, target = ?, target_source = ? WHERE window_open = ?",
                     (m["ticker"], target, target_source, key))
        S.db.commit()

    yes_ask = m.get("yes_ask") if m else None
    no_ask = m.get("no_ask") if m else None
    if no_ask is None and m and m.get("yes_bid") is not None:
        no_ask = 1 - m["yes_bid"]

    if rec["locked_side"] is None and secs_left >= pol.min_lead_s and (p >= pol.tau or p <= 1 - pol.tau):
        side = "ABOVE" if p >= 0.5 else "BELOW"
        S.db.execute("UPDATE windows SET locked_side=?, locked_prob=?, locked_at=?, locked_secs_left=?, "
                     "locked_price=?, yes_ask_at_lock=?, no_ask_at_lock=? WHERE window_open=?",
                     (side, p, now.isoformat(), secs_left, S.price, yes_ask, no_ask, key))
        S.db.commit()
        rec = S.db.execute("SELECT * FROM windows WHERE window_open = ?", (key,)).fetchone()

    if time.time() - S.last_snapshot >= 10:
        S.last_snapshot = time.time()
        S.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
                     (key, now.isoformat(), secs_left, S.price, p, m.get("yes_bid") if m else None, yes_ask))
        S.db.commit()

    def edge(prob, ask):
        return None if ask is None else prob - ask - fee_per_contract(ask)

    locked = rec["locked_side"]
    if locked:
        state = "LOCKED"
    elif secs_left < pol.min_lead_s:
        state = "NO_CALL"
    else:
        state = "WATCHING"
    return {
        "status": "ok",
        "state": state,
        "side": locked or ("ABOVE" if p >= 0.5 else "BELOW"),
        "prob_above": p,
        "locked_prob": rec["locked_prob"],
        "locked_at": rec["locked_at"],
        "locked_secs_left": rec["locked_secs_left"],
        "window_open": key,
        "window_close": (window_open + WIN).isoformat(),
        "secs_left": secs_left,
        "price": S.price,
        "target": target,
        "target_source": target_source,
        "distance_pct": (S.price / target - 1) * 100,
        "ticker": m["ticker"] if m else None,
        "yes_bid": m.get("yes_bid") if m else None,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "edge_above": edge(p, yes_ask),
        "edge_below": edge(1 - p, no_ask),
        "z": float(row["z"].iloc[0]),
        "model": S.model.kind,
        "tau": pol.tau,
        "min_lead_s": pol.min_lead_s,
        "kalshi_age_s": time.time() - S.market_ts if S.market_ts else None,
    }


def stats(month: str | None = None) -> dict:
    month = month or utcnow().strftime("%Y-%m")
    rows = S.db.execute("SELECT * FROM windows WHERE substr(window_open, 1, 7) = ?", (month,)).fetchall()
    return summarize_windows([dict(r) for r in rows], month)


def summarize_windows(rows: list[dict], label: str) -> dict:
    eligible = [r for r in rows if (r["first_seen_secs_left"] or 0) >= (r["min_lead_s"] or 90)]
    calls = [r for r in eligible if r["locked_side"]]
    settled = [r for r in calls if r["result"] in ("yes", "no")]
    correct = [r for r in settled if (r["locked_side"] == "ABOVE") == (r["result"] == "yes")]
    pnl, priced = 0.0, 0
    for r in settled:
        ask = r["yes_ask_at_lock"] if r["locked_side"] == "ABOVE" else r["no_ask_at_lock"]
        if ask is not None:
            won = (r["locked_side"] == "ABOVE") == (r["result"] == "yes")
            pnl += (1.0 if won else 0.0) - ask - fee_per_contract(ask)
            priced += 1
    n, k = len(settled), len(correct)
    from pipeline.evaluate import wilson
    lo, hi = wilson(k, n)
    return {
        "period": label,
        "windows_eligible": len(eligible),
        "calls": len(calls),
        "settled_calls": n,
        "correct": k,
        "accuracy": k / n if n else None,
        "acc_ci_low": lo if n else None,
        "acc_ci_high": hi if n else None,
        "coverage": len(calls) / len(eligible) if eligible else None,
        "pnl_1_contract": pnl if priced else None,
        "priced_calls": priced,
    }


def swiftbar_text(sig: dict, st: dict) -> str:
    green, red, gray, amber = "#10b981", "#f43f5e", "#a3a3a3", "#f59e0b"
    font = "font=Menlo size=12"
    if sig.get("status") != "ok":
        msg = {"no_data": "waiting for price", "no_target": "waiting for target", "starting": "starting"}
        return f"₿ … | color={gray} {font}\n---\n{msg.get(sig.get('status'), sig.get('status'))}\n" \
               f"Open dashboard | href=http://127.0.0.1:8765/\n"
    mm, ss = divmod(int(sig["secs_left"]), 60)
    clock = f"{mm}:{ss:02d}"
    p = sig["prob_above"]
    if sig["state"] == "LOCKED":
        arrow = "▲" if sig["side"] == "ABOVE" else "▼"
        conf = sig["locked_prob"] if sig["side"] == "ABOVE" else 1 - sig["locked_prob"]
        title = f"{arrow} {sig['side']} {conf:.0%} 🔒 {clock} | color={green if sig['side'] == 'ABOVE' else red} {font}"
    elif sig["state"] == "NO_CALL":
        title = f"· NO CALL {clock} | color={gray} {font}"
    else:
        lean = "▲" if p >= 0.5 else "▼"
        title = f"{lean} {max(p, 1 - p):.0%} wait {clock} | color={gray} {font}"

    open_et = pd.Timestamp(sig["window_open"]).tz_localize("UTC").tz_convert(ET)
    close_et = pd.Timestamp(sig["window_close"]).tz_localize("UTC").tz_convert(ET)
    lines = [title, "---",
             f"Window {open_et:%H:%M}–{close_et:%H:%M} ET | {font}",
             f"Target  ${sig['target']:,.2f} ({sig['target_source']}) | {font}",
             f"BTC     ${sig['price']:,.2f} ({sig['distance_pct']:+.3f}%) | {font}",
             f"P(above) {p:.1%}   lock at ≥{sig['tau']:.0%} | {font}"]
    if sig["yes_ask"] is not None:
        lines.append(f"Kalshi  YES {sig['yes_ask'] * 100:.0f}¢  NO {(sig['no_ask'] or 0) * 100:.0f}¢ | {font}")
        side = sig["side"]
        e = sig["edge_above"] if side == "ABOVE" else sig["edge_below"]
        if e is not None:
            # the model's opinion, not verified edge: out of sample, Kalshi's own price forecasts better
            lines.append(f"Model vs ask ({side}) {e * 100:+.1f}¢ after fee, unverified | color={gray} {font}")
    else:
        lines.append(f"Kalshi quote unavailable | color={amber} {font}")
    if sig["state"] == "LOCKED":
        ml, sl = divmod(int(sig["locked_secs_left"]), 60)
        lines.append(f"Locked with {ml}:{sl:02d} left at P={sig['locked_prob']:.1%} | {font}")
    lines.append("---")
    if st.get("settled_calls"):
        lines.append(f"{st['period']}: {st['correct']}/{st['settled_calls']} = {st['accuracy']:.1%} "
                     f"(95% CI {st['acc_ci_low']:.1%}–{st['acc_ci_high']:.1%}) | {font}")
        lines.append(f"Coverage {st['coverage']:.0%} of {st['windows_eligible']} windows | {font}")
        if st.get("pnl_1_contract") is not None:
            lines.append(f"P&L at lock price, 1 contract: ${st['pnl_1_contract']:+.2f} | {font}")
    else:
        lines.append(f"{st['period']}: no settled calls yet | {font}")
    base = os.environ.get("BTC_PUBLIC_URL", "http://127.0.0.1:8765")
    tok = f"?token={os.environ['BTC_API_TOKEN']}" if os.environ.get("BTC_API_TOKEN") else ""
    lines += ["---",
              f"Open Kalshi market | href=https://kalshi.com/markets/{SERIES.lower()}",
              f"Open dashboard | href={base}/{tok}"]
    return "\n".join(lines) + "\n"


async def tick_loop() -> None:
    while True:
        try:
            S.signal = compute_signal()
        except Exception as e:  # noqa: BLE001
            log.exception("signal: %s", e)
            S.signal = {"status": "error", "error": str(e)}
        await asyncio.sleep(1)


TRADER_MODE = os.environ.get("TRADER_MODE", "off")  # off | shadow | live
API_TOKEN = os.environ.get("BTC_API_TOKEN")          # required for every endpoint except /health when set
TRADER = None


def make_trader():
    if TRADER_MODE not in ("shadow", "live"):
        return None
    from .kalshi_client import KalshiClient
    from .trader import Config, Trader
    client = KalshiClient.from_env() if (TRADER_MODE == "live" or os.environ.get("KALSHI_KEY_ID")) else None
    if TRADER_MODE == "live":
        open_pos = [p for p in client.positions() if p["ticker"].startswith(SERIES)]
        if open_pos:
            raise RuntimeError(f"refusing to start live: existing {SERIES} positions {open_pos}")
    cfg = Config.load(TRADER_MODE, allocation=float(os.environ.get("TRADE_ALLOCATION", 10)),
                      stop_loss=float(os.environ.get("TRADE_STOP_LOSS", 5)),
                      hours=float(os.environ.get("TRADE_HOURS", 24)))
    return Trader(cfg, client, LIVE_DB_PATH)


async def trade_loop() -> None:
    while True:
        await asyncio.sleep(1)
        if TRADER is None:
            continue
        await asyncio.to_thread(TRADER.tick, dict(S.signal), time.time() - S.price_ts if S.price_ts else 999)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global S, TRADER
    S = State()
    log.info("model=%s tau=%.2f lead=%.0fs", S.model.kind, S.model.policy.tau, S.model.policy.min_lead_s)
    TRADER = make_trader()
    if TRADER:
        log.warning("TRADER %s: allocation $%.2f, stop at -$%.2f, params %s", TRADER.cfg.mode,
                    TRADER.cfg.allocation, TRADER.cfg.stop_loss, TRADER.cfg.params)
    tasks = [asyncio.create_task(f()) for f in (rest_loop, ws_loop, kalshi_loop, tick_loop, trade_loop)]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="btc-predict", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.middleware("http")
async def require_token(request: Request, call_next):
    """When BTC_API_TOKEN is set (any internet-facing deploy), every endpoint but /health needs it,
    as `Authorization: Bearer <token>` or `?token=<token>`."""
    if API_TOKEN and request.url.path != "/health" and not request.url.path.startswith("/assets"):
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
            request.query_params.get("token", "")
        page = request.url.path in ("/", "/index.html")
        if not page and not hmac.compare_digest(given, API_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/portfolio")
def get_portfolio() -> dict:
    if TRADER is None:
        return {"mode": "off"}
    return TRADER.status()


@app.post("/trading/stop")
def trading_stop() -> dict:
    if TRADER is None:
        return {"mode": "off"}
    TRADER.halt("stopped by user", dict(S.signal))
    return TRADER.status()


@app.post("/trading/start")
def trading_start() -> dict:
    if TRADER is None:
        return {"mode": "off"}
    TRADER.resume()
    return TRADER.status()


@app.get("/signal")
def get_signal() -> dict:
    return {**S.signal, "month": stats()}


@app.get("/swiftbar", response_class=PlainTextResponse)
def get_swiftbar() -> str:
    text = swiftbar_text(S.signal, stats())
    if TRADER is None:
        return text
    return portfolio_swiftbar(text, TRADER.status())


def portfolio_swiftbar(text: str, st: dict) -> str:
    """Prefix the menu-bar title with live P&L and add a portfolio section with a stop button."""
    green, red, gray = "#10b981", "#f43f5e", "#a3a3a3"
    font = "font=Menlo size=12"
    lines = text.rstrip("\n").split("\n")
    title, rest = lines[0], lines[1:]
    label, _, params = title.partition(" | ")
    pnl = st["pnl"]
    tag = "⏸" if st["halted"] else ("●" if st["mode"] == "live" else "◌")
    lines = [f"{tag} {pnl:+.2f} {label} | {params}", "---"]
    acct = st.get("account") or {}
    lines.append(f"Bot ({st['mode']}): ${st['equity']:.2f} of ${st['allocation']:.2f}  P&L {pnl:+.2f} | "
                 f"color={green if pnl >= 0 else red} {font}")
    if acct:
        lines.append(f"Kalshi account: ${acct['total']:.2f} (cash ${acct['cash']:.2f}) | {font}")
    if st["held"]:
        h = st["held"]
        lines.append(f"Holding {h['count']:.2f} {h['side'].upper()} @ {h['price'] * 100:.1f}¢ | {font}")
    lines.append(f"Trades: {st['trade_count']}   stop at ${st['stop_at']:.2f}   until {st['trade_until'][11:16]} UTC"
                 f" | color={gray} {font}")
    if st["halted"]:
        lines.append(f"HALTED: {st['halted']} | color={red} {font}")
    if st.get("last_error"):
        lines.append(f"error: {st['last_error'][:80]} | color={red} {font}")
    tok = f"?token={API_TOKEN}" if API_TOKEN else ""
    base = os.environ.get("BTC_PUBLIC_URL", "http://127.0.0.1:8765")
    if not st["halted"]:
        lines.append(f"⏹ Stop trading now | bash=/usr/bin/curl param1=-s param2=-X param3=POST "
                     f"param4={base}/trading/stop{tok} terminal=false refresh=true color={red}")
    return "\n".join(lines + rest[1:]) + "\n"


@app.get("/stats")
def get_stats(month: str | None = None) -> dict:
    return stats(month)


@app.get("/history")
def get_history(limit: int = Query(96, le=3000)) -> list[dict]:
    rows = S.db.execute("SELECT * FROM windows ORDER BY window_open DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/series")
def get_series() -> dict:
    """Current window's snapshots plus the last 60 one-minute bars, for the dashboard chart."""
    key = S.signal.get("window_open")
    snaps = [dict(r) for r in S.db.execute(
        "SELECT ts, secs_left, price, prob, yes_bid, yes_ask FROM snapshots WHERE window_open = ? ORDER BY ts",
        (key,)).fetchall()] if key else []
    bars = S.bars.tail(60).reset_index(names="ts")
    bars["ts"] = bars["ts"].astype(str)
    return {"snapshots": snaps, "bars": bars.replace({np.nan: None}).to_dict("records")}


@app.get("/research")
def get_research() -> dict:
    """Learning-loop scores, how much the recorder has collected, and the bot's shadow/live results."""
    from pipeline.config import DATA_DIR
    out: dict = {"loop": None, "recorder": {}, "bot": None}
    lj = DATA_DIR / "loop_latest.json"
    if lj.exists():
        out["loop"] = json.loads(lj.read_text())
    ticks = DATA_DIR / "ticks.sqlite"
    if ticks.exists():
        c = sqlite3.connect(f"file:{ticks}?mode=ro", uri=True, timeout=2)
        try:
            for t in ("spot", "kalshi_book", "kalshi_trades", "deribit", "brti", "kalshi_ob", "pmus_book"):
                try:  # max(rowid) is instant even on huge tables
                    out["recorder"][t] = c.execute(f"SELECT max(rowid) FROM {t}").fetchone()[0] or 0
                except sqlite3.Error:
                    out["recorder"][t] = None
            out["recorder"]["windows_settled"] = c.execute(
                "SELECT count(*) FROM kalshi_markets WHERE result IS NOT NULL").fetchone()[0]
            row = c.execute("SELECT min(ts) FROM kalshi_book").fetchone()
            out["recorder"]["since"] = pd.Timestamp(row[0], unit="ms").isoformat() if row and row[0] else None
        finally:
            c.close()
    from .arb_trader import summary as arb_summary
    try:
        out["arb"] = arb_summary()
    except sqlite3.Error:
        out["arb"] = None
    if TRADER is not None:
        st = TRADER.status()
        out["bot"] = {k: st[k] for k in ("mode", "equity", "pnl", "trade_count", "halted", "allocation")}
    return out


@app.get("/health")
def health() -> dict:
    return {"ok": True, "bars": len(S.bars), "price_age_s": time.time() - S.price_ts if S.price_ts else None,
            "kalshi_age_s": time.time() - S.market_ts if S.market_ts else None, "model": S.model.kind}


_dist = ROOT / "dashboard" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="dashboard")

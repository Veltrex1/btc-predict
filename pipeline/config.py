"""Shared paths and constants."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BTC_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("BTC_DB_PATH", DATA_DIR / "btc.duckdb"))
MODEL_PATH = Path(os.environ.get("BTC_MODEL_PATH", ROOT / "models" / "model.pkl"))
LIVE_DB_PATH = Path(os.environ.get("BTC_LIVE_DB_PATH", DATA_DIR / "live.sqlite"))
REPORT_DIR = ROOT / "reports"

WINDOW_MIN = 15
SERIES = "KXBTC15M"
KALSHI_BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
COINBASE_REST = "https://api.exchange.coinbase.com"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BINANCE_ARCHIVE = "https://data.binance.vision/data/spot"

# How the settlement price is approximated from 1-minute bars when Kalshi's own
# result is not available. Kalshi settles on CF Benchmarks BRTI; "avg60" treats
# the reference as a 60-second average (approximated by the bar's typical price),
# "close" uses the instantaneous price. Kalshi's result always wins when present.
DEFAULT_SETTLE_MODE = os.environ.get("BTC_SETTLE_MODE", "avg60")

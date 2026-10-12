# btc-predict

A Mac menu-bar widget that tells you whether to buy **ABOVE** or **BELOW** on Kalshi's 15-minute BTC market (`KXBTC15M`). Behind it is a local data pipeline, model and live logger, so the month-long accuracy can be measured against Kalshi's official results.

```
 ▲ ABOVE 84% 🔒 6:12        ← menu bar (SwiftBar)
 ─────────────────────
 Window 18:00–18:15 ET
 Target  $83,747.32 (kalshi)
 BTC     $83,802.10 (+0.066%)
 P(above) 91.2%   lock at ≥80%
 Kalshi  YES 88¢  NO 13¢
 Edge ABOVE +2.2¢/contract after fee
 Locked with 9:48 left at P=84.0%
 ─────────────────────
 2026-11: 412/519 = 79.4% (95% CI 75.7%–82.6%)
 Coverage 61% of 851 windows
```

## How it decides

Each Kalshi window settles YES if BTC at close is above the **target** set at open. Partway through a window, the important number is how far BTC sits from the target compared with how much it can still move in the time left:

```
z = ln(price / target) / (σ_1min · √minutes_left)        P(above) ≈ Φ(z)
```

- **Brownian baseline** (`p_bm`): the formula above, with σ from an EWMA of 1-minute returns. It needs no training, so the widget works on day one.
- **Gradient-boosted model** (`p_gbm`): adds momentum, the window's high/low range, longer-horizon volatility, order-flow imbalance and time of day, then isotonic calibration. It is used only if it beats the baseline out of sample.
- **Lock policy:** the first moment P(above) ≥ τ the widget locks **ABOVE**; at ≤ 1−τ it locks **BELOW**. The call never changes after that. If neither happens with at least `min_lead_s` (default 90s) left, the window is **NO CALL**. τ is chosen on validation months so the *lower* 95% confidence bound clears the accuracy target.

### Read this before presenting results

On a **pure random walk** (synthetic data with zero predictability, see `tests/`), this lock policy already scores about **80% accuracy on 98% of windows** (τ=0.74, calls lock around minute 5). High accuracy under "call any time, skipping allowed" rules comes mainly from the market's structure, not from forecasting skill. The Kalshi price reflects the same information: when you're 85% sure, YES usually costs about 85¢.

So the report also shows:
- **accuracy if forced to call every window at a fixed minute** (minute 0 is close to a coin flip),
- **edge at the Kalshi ask after fees**, plus **how often simply following the Kalshi favourite was right** on the same windows,
- **calibration** (are 80% calls right 80% of the time?).

Accuracy measures whether the probabilities are calibrated. Positive P&L at the ask is evidence of actual skill. Report both.

## Setup (on your Mac)

```bash
git clone <this repo> && cd btc-predict
curl -LsSf https://astral.sh/uv/install.sh | sh     # if you don't have uv
uv sync

# 1. Backfill data (all resumable; re-run any time to top up)
uv run python -m pipeline.backfill_kalshi --probe          # sanity-check Kalshi's field names
uv run python -m pipeline.backfill_kalshi --candles        # every settled KXBTC15M market + minute quotes
uv run python -m pipeline.backfill_coinbase --days 1095    # 3y of Coinbase 1m bars (~15 min)
uv run python -m pipeline.backfill_binance --start 2017-08 # optional: 9y of Binance 1m bars (~2 GB download)

# 2. Train + walk-forward backtest (prints validation choice and an untouched holdout month)
uv run python -m pipeline.train --years 3 --test-months 6
uv run python -m pipeline.evaluate
uv run python -m pipeline.report --backtest                 # reports/backtest.html

# 3. Dashboard + live server + menu-bar widget
(cd dashboard && npm install && npm run build)
./scripts/install_mac.sh        # launchd service on 127.0.0.1:8765 + SwiftBar plugin
open http://127.0.0.1:8765/     # dashboard
```

At the end of the graded month:

```bash
uv run python -m pipeline.report --live 2026-11   # reports/live-2026-11.html + .csv (every window, auditable)
```

## Data

| Source | What | Span | Used for |
|---|---|---|---|
| Kalshi API (public) | every settled `KXBTC15M` market: target, result, 1-min YES bid/ask | since the series launched | ground-truth labels, edge vs market |
| Coinbase Exchange | 1-min BTC-USD candles | ~3 years | features, proxy labels (Coinbase is in the BRTI index Kalshi settles on) |
| Binance archive | 1-min BTCUSDT + taker-buy volume | 2017-08 → now | longer feature history, order-flow features |

Everything is stored in `data/btc.duckdb`. Live calls go in `data/live.sqlite` (table `windows`, one row per window, holding the locked call and Kalshi's result).

**Settlement rule (from Kalshi's contract text):** YES if the simple average of the 60 seconds of CF Benchmarks BRTI before close is *at least* the same 60-second average before open (`strike_type: greater_or_equal`). BRTI isn't free, so when Kalshi's own result is missing, labels are approximated from Coinbase bars: `--settle avg60` (default) uses the typical price of the minute before open and before close, which approximates those 60-second averages; `close` uses the instantaneous price. `train.py` prints how often the proxy disagrees with Kalshi's official results.

**More history isn't always better:** BTC's volatility regime changes. More years help the slow features but can dilute recent behaviour. Compare `--years 2/3/5` in the walk-forward and keep whichever wins out of sample.

## Layout

```
pipeline/   backfills, labels, features, model, lock policy, evaluation, reports
server/     FastAPI live server (Coinbase websocket + Kalshi poll) and launchd plist
widget/     SwiftBar plugin (prints the server's /swiftbar text every 2s)
dashboard/  React dashboard (served by the server at / once built)
tests/      pytest: no-look-ahead, calibration on random walk, lock semantics, fees, Kalshi parsing, server
```

Run tests with `uv run pytest`.

This is a research tool. It never places orders. Kalshi's official result is the ground truth.

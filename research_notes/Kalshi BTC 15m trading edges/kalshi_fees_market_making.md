# Kalshi fees, maker incentives, market-making economics and API execution for KXBTC15M

Research date: 2026-10-12. Primary sources were Kalshi's public Trade API (queried live on 2026-10-12 around 02:00 UTC), docs.kalshi.com `.md` pages, and help.kalshi.com articles fetched with curl. The fee schedule PDF (kalshi.com/docs/kalshi-fee-schedule.pdf) sits behind a Vercel bot checkpoint, so it could not be read directly (see Gaps). WebFetch could not resolve kalshi.com, help.kalshi.com, polykal.net or pm.wiki, so curl was used for all of them.

## Q1. Do makers pay fees on KXBTC15M ("quadratic") versus "quadratic_with_maker_fees"? What is the current maker-fee policy, and which series have maker fees?

### Takeaway
Resting (maker) fills on KXBTC15M pay **no trading fee**. The series is `fee_type: "quadratic"`, which Kalshi's API spec maps to the General Trading Fees table only. Maker fees apply only to series flagged `quadratic_with_maker_fees` (163 of 14,948 series as of 2026-10-12, none of them 15-minute crypto) or `quadratic_with_combo_maker_fees`. KXBTC15M has no scheduled fee change. The cost to a maker is therefore adverse selection and inventory risk, not fees. Takers pay up to 1.75¢ per contract at P=0.50.

### Cited Findings
- The official API spec defines the fee types: "'quadratic' is described by the General Trading Fees Table, 'quadratic_with_maker_fees' is described by the General Trading Fees Table with maker fees described in the Maker Fees section, 'quadratic_with_combo_maker_fees' is the same maker-fee structure with a 0.5 maker multiplier instead of 0.25, 'flat' is described by the Specific Trading Fees Table." — [Kalshi API docs, Get Series](https://docs.kalshi.com/api-reference/market/get-series.md)
  - A "0.25 maker multiplier" on the 0.07 taker coefficient gives a maker coefficient of 0.0175. Combo series use 0.5, which gives 0.035.
- Live API on 2026-10-12: `GET /series/KXBTC15M` returns `fee_type: "quadratic"`, `fee_multiplier: 1`, `exchange_index: 2`, `frequency: "fifteen_min"`. The settlement source is the CF Benchmarks RTI, using the average of 60 RTI prices in the final minute. — [Kalshi API /series/KXBTC15M](https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M)
- Live API: `GET /series/fee_changes?series_ticker=KXBTC15M&show_historical=true` returns `{"series_fee_change_arr":[]}`, meaning no past or scheduled fee changes for KXBTC15M. — [Kalshi API fee_changes](https://api.elections.kalshi.com/trade-api/v2/series/fee_changes?series_ticker=KXBTC15M&show_historical=true)
- Live scan of all 14,948 series (`GET /series`) on 2026-10-12. Fee types: `quadratic`×1 on 14,753 series, `quadratic_with_maker_fees`×1 on 159, `quadratic`×0.5 on 18, `quadratic`×0 (fee-free) on 14, `quadratic_with_combo_maker_fees`×1 on 3, `quadratic_with_maker_fees`×0.5 on 1. Every 15-minute crypto series is `quadratic`×1: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M, KXDOGE15M, KXBNB15M, KXHYPE15M, KXADA15M, KXBCH15M, KXTON15M, KXZEC15M, KXNEAR15M, KXCRYPTOCOMP15M and KXCRYPTOLEAD15M. KXBTC and KXBTCD are also `quadratic`. — [Kalshi API /series](https://api.elections.kalshi.com/trade-api/v2/series)
- Examples of maker-fee series from the same scan: KXBTCMAX125, KXBTCMAX150, KXCPI, KXCPIYOY, KXFED, KXFEDDECISION, KXGDP, KXINXY, KXIPO, KXEPLGAME, KXLALIGAGAME, KXBUNDESLIGAGAME, KXATPMATCH, KXF1RACE, KXHEISMAN, KXINDY500, plus GPU-rental series (KXH100*, KXA100*, KXB200*). The fee_changes history shows several moved to `quadratic_with_maker_fees`, for example KXMLBGAME, KXMLBSERIES and KXWNBAGAME effective 2025-10-04. — [Kalshi API /series](https://api.elections.kalshi.com/trade-api/v2/series); [Kalshi API fee_changes](https://api.elections.kalshi.com/trade-api/v2/series/fee_changes?show_historical=true)
- Kalshi Help Center, Fees: "In some cases, markets will have maker fees associated with them. Maker fees are charged for orders placed that are not immediately matched and are instead left as resting orders on the orderbook. These fees are only charged when a trade is ultimately executed, there are no fees associated with canceling a resting order." — [Kalshi Help: Fees](https://help.kalshi.com/en/articles/13823805-fees)
- Third-party summary of the July 2026 fee schedule (headed "Fee Schedule for July 2026 - 7.7.26 Update"):
  - taker fee = round up(M × 0.07 × C × P × (1−P))
  - maker fee = round up(M_maker × 0.0175 × C × P × (1−P))
  - "M_maker defaults to 0: on a standard series makers pay nothing. Maker fees apply only to the series listed under non-standard fees in the schedule, which the API flags as fee_type = quadratic_with_maker_fees."
  - KXMVE combos are reported as maker 2 / taker 1.

  — [PolyKal: Kalshi Fees Explained](https://polykal.net/kalshi-fees-explained/) (secondary; consistent with the official API spec above). The PDF title appears in search results for [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf).
- Fee rounding is in the official docs:
  - The trade fee is `ceil_6dp(model_fee)`.
  - A "rounding fee" restores the user's balance precision: $0.0001 for direct members, $0.01 for non-direct (FCM-cleared) members.
  - Overpaid rounding is rebated through a per-order accumulator that "is maintained per order across all fills regardless of whether the fills are taker or maker."
  - Net fee = trade fee + rounding fee − rebate (always ≥ $0).

  — [Kalshi docs: Fee Rounding](https://docs.kalshi.com/getting_started/fee_rounding.md)

### Inferences
- Taker fee per contract on KXBTC15M at mid-price P: 0.07·P(1−P) = 1.75¢ at P=0.50, 1.68¢ at 0.40/0.60, 1.47¢ at 0.30/0.70, 0.63¢ at 0.10/0.90. Direct members pay roughly this exact amount thanks to $0.0001 precision. Non-direct members get rounding to the cent, with the accumulator rebating it over fills.
- A maker earning a 1¢ quoted spread (0.5¢ per side against mid) pays no fee. A taker crossing that spread pays about 0.5¢ of half-spread plus up to 1.75¢ in fees, so a taker needs about 2.25¢ of edge per contract at P≈0.5. That fee wall protects passive makers against slow or uninformed takers. It does not protect them from latency traders whose edge is larger, for example after a fast BRTI move late in the window.
- Self-matching your own resting order is blocked by self-trade prevention (see Q5), so a maker cannot round-trip with itself to farm anything.

### Gaps
- I could not read the official fee schedule PDF verbatim because of the Vercel bot challenge for curl and DNS failure for WebFetch. The 0.0175 maker coefficient is confirmed only indirectly: the API spec states a 0.25 multiplier, and secondary sources give 0.0175.
- Event-level fee overrides exist (`GET /events/fee_changes`, "an override layered on top of the parent series' fee structure"). I did not check whether any KXBTC15M event carries one. Nothing indicates it, but a quick `GET /events/fee_changes` check before deploying is recommended. — [Kalshi docs: Get Event Fee Changes](https://docs.kalshi.com/api-reference/events/get-event-fee-changes.md)
- Designated market makers "may receive reduced fees" (see Q2), but the terms are private.

## Q2. Is there a Liquidity Incentive Program, market-maker program or Volume Incentive Program that applies to 15-minute crypto markets?

### Takeaway
Kalshi runs three relevant programs:

1. **Liquidity Incentive Program (LIP).** Public, open to most U.S. retail members. It pays per-market reward pools for resting depth near a reference price, scored on 1-second snapshots. It runs until 2027-01-01.
2. **Designated Liquidity Provider / Market Maker programs.** These require a signed Market Maker Agreement. The help center lists "Crypto 15min markets" with **2 designated providers** through 12/31/26.
3. **Volume incentives.** The API supports `volume` incentive types.

In a scan of roughly 17,000 incentive programs from the public API, I found **no LIP or volume program on any KXBTC15M market**. The only 15-minute crypto LIP found was on KXCRYPTOLEAD15M, at $20 per 15-minute period. A retail market maker on KXBTC15M should therefore expect **no rebate or incentive income**. The incumbent competition is the 2 contracted designated liquidity providers.

### Cited Findings
**LIP terms (official help center, "Updated this week")**
- "The Liquidity Incentive Program rewards you for placing resting orders that improve market liquidity... even if your orders don't get filled." Program end: January 1, 2027. "Kalshi can end or modify the program at any time." — [Kalshi Help: Liquidity Incentive Program](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
- Eligibility: "Most regular U.S. Kalshi members". The following cannot participate: "Kalshi affiliates and employees; Introducing Brokers, FCMs, and their customers; International, non-U.S. users". — [same](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
- Scoring rules (all from [same](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)):
  - Kalshi takes a snapshot "once per second, at a random moment within each second".
  - Reference Price: "Walking down from the best bid, it is the first price level at which cumulative resting size reaches one fifth of the Target Size."
  - Distance penalty: orders at or better than the reference get 1.0×. Orders below it get "the Discount Factor raised to the number of ticks away".
  - Raw score = Order Size × Distance Multiplier, normalized per side. A snapshot score is your share of the yes side plus your share of the no side, so at most 2.0.
  - "If a side of the book never reaches the Target Size, no orders on that side qualify". A snapshot is excluded if there is not two-sided liquidity meeting Target Size on both sides.
  - Reward = Time Period Score × Time Period Reward × (non-excluded snapshots ÷ total snapshots), rounded down to the cent.
- Parameters (from [same](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)):
  - Target Size is "more than 100 and fewer than 20,000 contracts".
  - Discount Factor is "up to 1.00".
  - Time periods are up to 31 days and may overlap.
  - "Daily rewards: $1-$1,000 per market, per day". Minimum payout $1.00.
  - Kalshi "monitors for abusive behavior and market manipulation".
- Payment timing: "Rewards are not credited in real time. Final scoring occurs after a program ends, and payment follows in a later processing run." Program definitions are available through the public Trade API, and the Rewards page is at kalshi.com/incentives. — [Kalshi Help: LIPs, where to find them](https://help.kalshi.com/en/articles/16076644-liquidity-incentive-programs-where-to-find-them)
- A third-party model built from the CFTC filing (clean terms effective 2026-07-30) adds that payout = pool × 0.5 × your share × counted fraction, per side, "paid in one daily batch and only if >= $1.00". On one $20 account over Oct 2–10, 2026, rewards were $20.30 against trading losses of $36.06: "the payout is predictable, the fill risk is the hard part (fast traders sweep resting cheap bids)". This is a single-user report and unverified. — [GitHub: mirabilis-agent/lip-ledger](https://github.com/mirabilis-agent/lip-ledger)

**LIP coverage, from the live API (`GET /incentive_programs`, 2026-10-12)**
- The endpoint supports types `liquidity`, `volume`, `margin_maker_volume` and `margin_taker_volume`. Fields include `period_reward` (in centi-cents), `discount_factor_bps` and `target_size_fp`. — [Kalshi docs: Get Incentives](https://docs.kalshi.com/api-reference/incentive-programs/get-incentives.md)
- Active programs: 4,686, all of type `liquidity` with description `series_lip`. The most common settings were:
  - period_reward of 1,000,000 centi-cents ($100), 3,428 programs
  - discount_factor_bps of 5000 (0.5)
  - target_size of 1,000 contracts

  — [Kalshi API incentive_programs?status=active](https://api.elections.kalshi.com/trade-api/v2/incentive_programs?status=active)
- Scan of active (4,686), upcoming (2,415), closed (13,340) and paid_out (first 1,000 rows) programs, with start dates from 2026-03-12 to 2026-11-30:
  - **0 programs on any KXBTC15M market.**
  - The only crypto 15-minute programs were on KXCRYPTOLEAD15M (BTC/ETH/SOL "leader" markets): 1,348 of them, each worth 200,000 centi-cents ($20) per 15-minute window, target size 1,000, discount factor 0.5.

  — [Kalshi API incentive_programs](https://api.elections.kalshi.com/trade-api/v2/incentive_programs)
- The volume-type programs I found were on other series, for example KXMLBSEASONGAMES at $500 per period in September 2026. — [Kalshi API incentive_programs?type=volume](https://api.elections.kalshi.com/trade-api/v2/incentive_programs?status=all&type=volume)

**Designated liquidity provider / market maker programs**
- Liquidity Provider Program: "Members who have executed a Market Maker Agreement with Kalshi may become a Designated Liquidity Provider for the Incentivized Series... If a Designated Liquidity Provider meets the 'Incentive Period Requirements,' they will receive the 'Incentive Period Reward.'" Selection is by "auction" on the minimum reward you will accept. The Active Programs table lists "**Crypto 15min markets | 12/31/26 | 2**" providers and "Crypto Hourly & Daily | 12/31/26 | 1". It also covers BTC/ETH/etc. perps with up to 6 providers. — [Kalshi Help: Liquidity Provider Program](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program)
- Market Maker Program: "designated market makers who agree to provide consistent, two-sided liquidity. In exchange for meeting defined quoting and volume requirements, market makers may receive reduced fees and certain adjusted position limits." Status "is granted following a thorough review of financial resources, trading experience, and business reputation". The covered-products list includes KXBTC, KXBTCD, KXETH, KXETHD and others, with "98% of each 1h increment" availability. KXBTC15M is not in that list. — [Kalshi Help: How to become a market maker](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi)
- An older Kalshi notice titled "Volume and Liquidity Incentive Program - August 2025" exists on Kalshi's public S3 regulatory notices. I did not read it, and it predates the current terms, so it may be outdated. — [Kalshi notice PDF (Aug 2025)](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Volume%20and%20Liquidity%20Incentive%20Program%20-%20August%202025.pdf)

### Inferences
- With no LIP on KXBTC15M, a retail maker's only revenue is spread capture. The book's "thousands of contracts at the touch" is consistent with the 2 designated crypto-15-minute providers quoting under contract with an incentive-period reward. A retail bot joins the back of their queue at 1¢ spreads.
- KXCRYPTOLEAD15M LIP at $20 per 15 minutes is about $1,920/day of headline pool, if every window is incentivized. Each window pays out only if your share × counted fraction × pool ≥ $1, and the third-party model applies a 0.5 per-side factor. It could be a small adjacent opportunity, but it was not studied here.
- Becoming a designated provider (MM Agreement plus auction) is the only path to incentive money on 15-minute crypto. It needs institutional review.

### Gaps
- The terms of the crypto-15-minute Designated Liquidity Provider agreement (quoting obligations, reward size) are not public.
- The paid_out scan was truncated at 1,000 rows by a pagination error, so historical KXBTC15M programs before about 2026-03 cannot be fully excluded.
- I found no CFTC filing text for the current LIP; the 2026-07-30 effective date is from a secondary source.

## Q3. What is known about market-making economics on Kalshi and Polymarket short-dated crypto markets? Who are the existing market makers?

### Takeaway
No public source gives audited P&L for a market maker on Kalshi 15-minute crypto markets. The evidence that exists points to adverse selection from latency or informed takers as the dominant cost:

- Polymarket added taker fees to its 15-minute crypto markets in January 2026 specifically to blunt latency arbitrage and to fund maker rebates.
- Small-account reports (one on Kalshi LIP, one RSI bot backtest) show losses driven by fill or selection risk.

Known Kalshi market makers include Susquehanna (SIG), designated since April 2024. Kalshi also says it has 2 designated providers for crypto 15-minute markets, but does not name them.

### Cited Findings
- Susquehanna Government Products (an SIG subsidiary) set up a trading desk as a market maker on Kalshi in April 2024, covering events "from Bitcoin to Taylor Swift". This predates 2025 and is possibly outdated regarding current scope. — [Bloomberg, 2024-04-03](https://www.bloomberg.com/news/articles/2024-04-03/susquehanna-starts-trading-desk-for-event-contracts-on-kalshi); [Bloomberg Law](https://news.bloomberglaw.com/crypto/susquehanna-starts-trading-desk-for-event-contracts-on-kalshi)
- Kalshi says: "There are a number of market makers on Kalshi, primarily individuals and small institutions... the majority of participants on Kalshi today are retail traders." The article is undated and possibly older. — [Kalshi Help: Who are you trading with](https://help.kalshi.com/en/articles/13823808-who-are-you-trading-with)
- Polymarket 15-minute crypto: reporting from January 2026 says taker-only fees were introduced to fund maker rebates and curb latency arbitrage. "Bots monitored small delays between Polymarket's internal pricing and spot prices on major crypto exchanges... entered trades when odds hovered near 50/50". — [Finance Magnates via TradingView](https://www.tradingview.com/news/financemagnates:ab852684e094b:0-polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/); [Cointelegraph via TradingView](https://www.tradingview.com/news/cointelegraph:e59c32089094b:0-polymarket-quietly-introduces-taker-fees-on-15-minute-crypto-markets/)
- Polymarket's current docs:
  - Crypto taker fee rate is 0.07, maker fee rate is 0.
  - The fee is C × feeRate × p × (1−p), the same functional form as Kalshi.
  - The **Crypto maker rebate is 20%** of taker fees, "fee-curve weighted", paid daily in pUSD with a $1 minimum.
  - "You earn based on the share of liquidity you provided that actually got taken."

  — [Polymarket docs: Maker Rebates Program](https://docs.polymarket.com/polymarket-learn/trading/maker-rebates-program)
- A vendor blog cites a Stanford Law study (Robert Bartlett, April 2026, 41.6M Kalshi trades). It reports that informed traders "systematically pick off slow quotes, and market makers compensate by widening spreads — particularly in single-name contracts". I have not verified the study itself; this is a secondary vendor source. — [TurbineFi blog](https://www.turbinefi.com/blog/why-prediction-market-trades-get-picked-off-2026)
- KXBTC15M open-source RSI bot, paper or modeled fills, 21-day backtest from 2026-09-14 to 2026-10-05:
  - Total PnL −$2,386.62, profit factor 0.786, fees ≈ $715.
  - It ships a "conservative model with queue position, partial fills, and adverse selection" next to the optimistic fill model.
  - The author notes that historical order books are unavailable, so entry prices are proxied.

  — [GitHub: nepalanurag/kalshi-btc15m-bot](https://github.com/nepalanurag/kalshi-btc15m-bot)
- Open-source Kalshi market-making code (none shows verified live P&L):
  - [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker), about 415 stars, created 2024 ("simple market making strategies")
  - [guzus/dr-manhattan](https://github.com/guzus/dr-manhattan), a CCXT-style multi-venue library covering market making
  - [nikhilnd/kalshi-market-making](https://github.com/nikhilnd/kalshi-market-making), a 2023 student project, outdated
  - [moomooskycow/atreides](https://github.com/moomooskycow/atreides), Avellaneda-Stoikov on Kalshi
  - [zachdaube/kalshi-market-maker](https://github.com/zachdaube/kalshi-market-maker)

  Source for the list: [GitHub search](https://github.com/search?q=kalshi+market+making&type=repositories)
- A commercial backtest vendor claims that 2,154 of 5,732 simulated Kalshi BTC 15-minute bot variants were profitable. This is simulated, vendor-sourced and weak evidence. — [TurbineFi: 5,732 backtests](https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026)

### Inferences
**Economics for one round trip at P≈0.5 with a 1¢ spread**
- Gross capture is about 1¢ per buy+sell pair, or 0.5¢ per fill relative to mid. There are no maker fees.
- The binary's delta to BTC is very large near expiry. Over the final minutes, a 0.1% BTC move can shift fair value by tens of cents. Fills are concentrated exactly when fair value has moved, which is classic adverse selection.
- Break-even requires average adverse move per fill < 0.5¢. With BRTI (the settlement index) streaming at 5 Hz on Kalshi's own WebSocket, and spot venues updating faster, a REST/WS retail bot will usually be slower than the designated providers and latency takers.

**Inventory risk**
- Each 15-minute market settles to $0 or $1, so unhedged inventory at expiry is a full binary bet.
- Hedging requires BTC perps/spot (Kalshi lists KXBTCPERP) or the opposite-side order. YES bid at x plus NO bid at (1−x−spread) is equivalent to a two-sided quote, and holding both YES and NO nets to $1.

**Overall view**
- The thick touch (thousands of contracts) suggests large incumbents with queue priority. A new 1-contract-scale maker gets filled mostly when the touch is about to be run over, which is again adverse selection.
- Passive market making on KXBTC15M without speed or alpha advantages is likely unprofitable or marginal. This inference is consistent with all the anecdotal evidence above, but it is not proven.

### Gaps
- I found no published live P&L or fill-toxicity statistics for KXBTC15M market making.
- I found no identification of the 2 designated crypto-15-minute providers.
- I could not access the Stanford study directly.
- Reddit, X and Substack threads were not examined in depth.

## Q4. Kalshi WebSocket and execution API: channels, auth, rates, amend/decrease, order groups, FIX, colocation

### Takeaway
The REST v2 and WebSocket APIs give a retail account everything a market maker needs:

- **REST v2:** create, batch create, amend (price, count, expiration), decrease, batch cancel, post_only, self-trade-prevention mode, cancel_order_on_pause and order groups (a rolling 15-second fill-limit kill switch).
- **WebSocket:** authenticated orderbook_delta, ticker, trade, fill, user orders, market_positions, lifecycle and order_group_updates channels, plus a **CF Benchmarks BRTI feed at up to 5 Hz**, which is the settlement index.
- **FIX 5.0SP2:** order-entry (NR) and market-data sessions using the same API keys, which appears self-serve. Some sessions (RT, drop copy, post-trade) are gated.
- **Connectivity:** AWS PrivateLink needs Premier tier or above; VPC peering needs Prime or above. There is no classic colocation.

Basic tier write capacity is about 10 order actions/second, with cheaper cancels.

### Cited Findings
**Endpoints and sharding**
- REST: `https://external-api.kalshi.com/trade-api/v2`, with api.elections.kalshi.com also supported.
- WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, with `wss://api.elections.kalshi.com/trade-api/ws/v2` still supported.
- Demo: `external-api(-ws).demo.kalshi.co`.

— [Kalshi docs: Quick Start WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets.md); [Get Incentives spec servers](https://docs.kalshi.com/api-reference/incentive-programs/get-incentives.md)
- KXBTC15M is on `exchange_index: 2`. "Programmatic traders must preallocate collateral on a given exchange shard before order placement". Transfers use the Intra Account Transfer API, and opt-in auto-rebalancing runs every 10 seconds. — [Kalshi docs: Exchange Sharding](https://docs.kalshi.com/getting_started/exchange_sharding.md); [API /series/KXBTC15M](https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M)

**WebSocket**
- Auth: "Authentication is required to establish the connection; include API key headers during the WebSocket handshake." — [Kalshi docs: WebSocket Connection](https://docs.kalshi.com/websockets/websocket-connection.md)
- Channels: "Private channels: `orderbook_delta`, `fill`, `market_positions`, `communications`, `order_group_updates`" and "Public market-data channels: `ticker`, `trade`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `multivariate`". — [Quick Start WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets.md)
- The llms.txt index also lists User Orders, CF Benchmarks Value, CF Benchmarks 5Hz Value, and Pyth Value channels. — [docs llms.txt](https://docs.kalshi.com/llms.txt)
- orderbook_delta "Sends `orderbook_snapshot` first, then incremental `orderbook_delta` updates". It supports `update_subscription` with `add_markets` / `delete_markets` / `get_snapshot`. — [Kalshi docs: Orderbook Updates](https://docs.kalshi.com/websockets/orderbook-updates.md)
- CF Benchmarks feed: "Real-time CF Benchmarks index value updates at up to 5 updates per second" (5Hz channel). The standard channel carries "trailing 60-second and quarter-hour final-minute averages". BTC index ID is `BRTI`. — [CF Benchmarks Value Feed](https://docs.kalshi.com/websockets/cfbenchmarks-value.md); [5Hz feed](https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz.md)
- Margin WebSocket docs say Kalshi sends Ping frames every 10 seconds with body `heartbeat`. I assume the same applies to the event WebSocket (not verified). — [Margin WS keep-alive](https://docs.kalshi.com/margin-ws/websockets/connection-keep-alive.md)

**Orders**
- Create Order V2 (`/portfolio/events/orders`) fields include:
  - `post_only`
  - `self_trade_prevention_type`, required, one of `taker_at_cross | maker`
  - `cancel_order_on_pause`
  - `reduce_only`
  - `order_group_id`
  - `subaccount`
  - `exchange_index`
  - prices as fixed-point dollars (e.g. "0.5600")
  - fractional counts

  The legacy `/portfolio/orders` endpoint "will be deprecated no earlier than May 6, 2026". — [Kalshi docs: Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2.md)
- Amend Order V2 changes "the price, max fillable count, and/or expiration time". The request `count` is the total, equal to filled plus desired resting. Decrease Order V2 takes "Exactly one of `reduce_by` or `reduce_to`". Batch Create and Batch Cancel V2 "maximum batch size scales with your tier's write budget". — [docs llms.txt](https://docs.kalshi.com/llms.txt); [Amend Order V2](https://docs.kalshi.com/api-reference/orders/amend-order-v2.md); [Decrease Order V2](https://docs.kalshi.com/api-reference/orders/decrease-order-v2.md)
- Order groups: "automatic order cancellation when a contracts limit is reached within a rolling 15-second window". The limit is 1–1,000,000. Groups can be manually triggered (cancels all), reset, or deleted, and you can have up to 100,000 groups. — [Kalshi docs: Order Groups](https://docs.kalshi.com/getting_started/order_groups.md)

**Rate limits**
- Token buckets:
  - Basic: 200 read / 100 write tokens per second.
  - Advanced: 300/300, via a self-serve upgrade endpoint.
  - Expert: 600/600.
  - Premier: 1,200/1,200.
  - Paragon: 2,400.
  - Prime: 4,800.
  - Prestige: 12,000/9,600.
- Higher tiers are earned by 30-day volume share. Expert needs 0.075% to earn and 0.05% to keep; Prestige needs 1.00%.
- Basic-tier Write buckets "hold one second of budget". Higher tiers burst up to 3×.
- A batch of 25 cancels costs 25 × 2 = 50 tokens, so a cancel costs 2 tokens versus 10 for a create.
- A 429 response carries no Retry-After header and no penalty.

— [Kalshi docs: Rate Limits and Tiers](https://docs.kalshi.com/getting_started/rate_limits.md)

**FIX and connectivity**
- Order Entry `mm.fix.elections.kalshi.com`: port 8228 (KalshiNR) and port 8230 (KalshiRT, retransmission). RT, Drop Copy and Post Trade need you to "Contact institutional@kalshi.com".
- Market Data `marketdata.fix.elections.kalshi.com:8233`.
- FIXT.1.1/FIX50SP2 over TLS 1.2+.
- "FIX API keys use the same key pair as the REST API". One FIX connection per API key.
- FIX uses the same token buckets as REST. Mass Cancel (35=q) is limited to 1/sec.

— [Kalshi docs: FIX Connectivity](https://docs.kalshi.com/fix/connectivity.md); [FIX Authentication](https://docs.kalshi.com/fix/authentication.md)
- "Members on the Premier tier or above can contact institutional@kalshi.com to provision a PrivateLink endpoint". "Members on the Prime tier or above can also... discuss VPC peering". FIX traffic then stays on the AWS backbone. — [FIX Connectivity](https://docs.kalshi.com/fix/connectivity.md)

**Maintenance**
- Every Thursday from 3:00 to 5:00 AM ET there is a trading pause. You cannot place or amend orders but you can cancel, and resting orders remain unless CancelOrderOnPause is set. — [Kalshi docs: Maintenance and Pauses](https://docs.kalshi.com/getting_started/maintenance_and_pauses.md)

### Inferences
- At Basic tier with 100 write tokens/s and a one-second bucket, you get about 10 creates or amends per second, or about 50 cancels per second. That is enough to requote a couple of KXBTC15M markets on each BRTI tick, but not to compete on speed. Upgrading to Advanced (300/s, 3-second burst) is self-serve and worth doing.
- Kalshi hosts on AWS: the docs reference AWS NLB TLS policies and PrivateLink. Running a bot in AWS us-east-1 is the closest practical "colocation" for retail. This is an inference; Kalshi does not publish its region.
- Order groups with a small contracts limit act as a cheap fill-burst kill switch against being swept. That is directly useful against adverse selection.

### Gaps
- I found no published WebSocket message-rate caps or typical latency figures in the docs.
- I did not check whether FIX NR access is fully self-serve for Basic-tier retail accounts in practice.
- Exact token costs for amend and decrease come from `GET /account/endpoint_costs`, which needs auth and was not queried.

## Q5. Rules on automated trading, self-trade and market making by retail API users

### Takeaway
API and automated trading by retail is explicitly supported (self-serve API keys, tiers earned by volume). Self-trade is prevented mechanically: `self_trade_prevention_type` is a required order field. Wash trading and manipulation are prohibited and monitored, and the LIP rules specifically warn about abusive behavior. I did not find any rule restricting retail from market making. Note that crypto contracts carry a prohibition for people holding material non-public information on the underlying.

### Cited Findings
- `self_trade_prevention_type` is required on Create Order V2, with valid values `taker_at_cross` or `maker`. — [Kalshi docs: Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2.md)
- KXBTC15M `additional_prohibitions`: "Persons who are employed by any of the Source Agencies are not permitted to trade on the Contract." and "Persons who hold any material, non-public information on the Underlying are not permitted to trade on the Contract." — [Kalshi API /series/KXBTC15M](https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M)
- LIP: "Kalshi monitors for abusive behavior and market manipulation; Participant status can be revoked for violations". — [Kalshi Help: LIP](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
- Kalshi publicly states that self-trading is mechanically blocked and coordinated wash trading is prohibited and monitored. This came up in the context of 2026 WSJ reporting on clustered ETH-perp trades; no regulator finding has been made. — [Crypto Briefing](https://cryptobriefing.com/cftc-kalshi-scrutiny-ether-volume/); [Parameter](https://parameter.io/kalshi-confronts-wash-trading-accusations-linked-to-ethereum-perpetual-contracts/)
- A CFTC FOIA document asks Kalshi to specify rulebook prohibitions on wash trading and front-running. The date is unclear and it is possibly pre-2025. — [CFTC FOIA](https://cftc.gov/media/7566/FOIA_2100068/download)
- The exchange-sharding docs say market makers can disable automatic shard transfers, which shows the API is designed for programmatic market makers. — [Exchange Sharding](https://docs.kalshi.com/getting_started/exchange_sharding.md)

### Inferences
- A retail API market maker is permitted. The practical constraints are self-trade prevention (choose `maker` mode to cancel your resting order rather than the incoming one), anti-spoofing and wash norms (do not post orders you intend to cancel to move prices), and the MNPI clause.

### Gaps
- I did not read the current KalshiEX Rulebook text on spoofing, wash trading and message-to-trade ratios. The CFTC-hosted copy found was v1.13 from August 2024, possibly outdated.
- I found no explicit order-to-trade ratio or cancel-rate limits beyond the token buckets.

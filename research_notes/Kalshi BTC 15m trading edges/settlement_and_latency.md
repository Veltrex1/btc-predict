# Settlement-Index Mechanics and Lead-Lag / Latency Edges for Kalshi KXBTC15M

Researched 2026-10-12. Primary sources: CF Benchmarks methodology PDFs (versions dated Aug–Sep 2026) and docs.kalshi.com (fetched with curl on 2026-10-12). Practitioner sources about latency are mostly vendor or bot-repo marketing and are flagged as such.

Contract rule (given by coordinator, from Kalshi market data): Yes if avg(BRTI, 60 s before close) >= avg(BRTI, 60 s before open). Final value = mean of 60 RTI prints, rounded to 2 dp.

## 1. BRTI methodology: constituents, calculation, frequency, deviation from single venues

### Takeaway
BRTI is computed **every 200 ms** (changed from once per second in methodology v16.6, 18 May 2026). It comes from a consolidated, size-capped and (since 29 Jun 2026) uncrossed order book across **7 US-dollar spot venues**: Bitstamp, Coinbase, Kraken, Gemini, LMAX Digital, Bullish and Crypto.com. itBit was suspended on 27 Jul 2026. The index is not a "median of volume-weighted mids". It is an exponentially weighted average of the consolidated mid price-volume curve, taken out to a "utilized depth" where the ask curve is within 0.5% of mid. **Binance, OKX, Bybit and all perpetual futures are not inputs.**

### Cited Findings
- BRTI constituent exchanges (Constituent Exchanges List v13.7, 24 Aug 2026): Bitstamp (added 14-Nov-16), Coinbase (14-Nov-16), Kraken (14-Nov-16), Gemini (30-Aug-19), LMAX Digital (03-May-21), Bullish Exchange (30-Dec-24), Crypto.com (31-Mar-25). itBit's BRTI membership is marked "Suspended 27-Jul-26" (v13.6 change log: "Suspension of itBit Paxos from … CME CF Bitcoin Real Time Index – BRTI"). Bitfinex and OKCoin were dropped in Apr 2017. — [CME CF Constituent Exchanges PDF](https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf)
- A secondary glossary lists the 2026 constituents as "Bitstamp, Coinbase, Gemini, itBit, Kraken, LMAX". It is out of date: it misses Bullish and Crypto.com and still includes itBit. — [learnbitcoin.com glossary](https://www.learnbitcoin.com/glossary/cme-cf-bitcoin-reference-rate); contradicted by [CF Constituent Exchanges PDF](https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf)
- Calculation steps (RTI Methodology, Section 4.1):
  1. At each Effective Time, every constituent's order book is added to a joint list.
  2. The books are consolidated, with each price level capped at the "order size cap" (Dynamic for BTC).
  3. The consolidated book is **uncrossed**: while best bid >= best ask, the smaller size is removed from both sides.
  4. Bid, ask, mid price-volume curves and the mid spread-volume curve are built at the "spacing" granularity (Dynamic).
  5. Utilized depth = the maximum cumulative volume at which the mid-spread curve stays within deviation D of mid.
  6. The mid price-volume curve is weighted by a normalized exponential density up to the utilized depth, then summed.

  — [CME CF Real Time Indices Methodology](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)
- BRTI parameters (Section 6.2 table): Spacing = Dynamic; Deviation from mid D = **0.5%** (ETH/SOL/XRP use 1%); Potentially Erroneous Data Parameter = **5%**; Order size cap = Dynamic; Effective Time = **every 200 milliseconds**; Retrieval Lag Threshold = **10 seconds** (smaller RTIs such as LTC are still "every second", with a 30 s threshold). — [RTI Methodology](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)
- Change log:
  - v16.6 (18 May 2026) updated the Effective Time and Retrieval Lag Threshold for BRTI, ETH, SOL and XRP.
  - v16.7 (8 Jun 2026) made spacing Dynamic for all RTIs.
  - v16.8 (29 Jun 2026) added the order-book uncrossing step.
  - v17.0 (21 Sep 2026) updated the Dynamic Spacing description.

  — [RTI Methodology](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf); consultation notice (10 Jun 2026; consultation closed 24 Jun; implementation planned 29 Jun 2026 11:00 London; before the change, "crossed levels are retained and contribute to the price-volume (PV) curve") — [CF Benchmarks announcement](https://www.cfbenchmarks.com/blog/announcement-of-consultation-on-changes-to-the-cme-cf-real-time-indices-methodology)
- Contingency rules:
  - A venue whose book is more than 10 s stale is dropped for that calculation.
  - A crossed single-venue book is flagged erroneous and dropped.
  - A venue whose mid deviates more than 5% from the median of constituent mids is dropped. It is reinstated only when the deviation falls below 2.5%.
  - If no valid input remains, no value is published for that time.

  — [RTI Methodology §5](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)
- CF Benchmarks says that because BRTI uses only order-book data (no trades, no trailing window), it is "both a Markov process and a martingale … the best prediction of the next … value is its current value". — [RTI Methodology §4.2](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)
- The BRTI product page says "once per 200 millisecond benchmark index price" and also "Calculated every second", which is inconsistent page copy. The page calls BRTI "the price input for settlement of event contracts and prediction markets offered by Kalshi, CME Group and ForecastEX". It showed BRTI at $83,558.20 on 12 Oct 2026 02:02:45 GMT. — [CF Benchmarks BRTI page](https://www.cfbenchmarks.com/data/indices/BRTI)

### Inferences
- BRTI is a depth-weighted consolidated mid of **USD spot books on US/regulated venues**. It should track the Coinbase mid closely, because Coinbase is likely the deepest USD book. It will differ structurally from Binance BTCUSDT by the USDT/USD basis, and from perpetual futures by the funding/basis. Using Binance or perps as a proxy for the settlement value means modelling that basis. The basis is typically a few bps but can jump during USDT stress.
- Because the index is a weighted average over depth with exponential weights, it is smoother than any single venue's top-of-book. Brief one-venue spikes or wicks move it only partly. Liquidation wicks on Binance perps should not be read 1:1 into BRTI.
- The 200 ms cadence (since May 2026) matters for the settlement average. See section 3 for the open question of which tick within each second Kalshi samples.

### Gaps
- I found no published statistic on the typical dollar deviation between BRTI and Coinbase mid, or between BRTI and Binance BTCUSDT, at 1-second resolution. This needs measuring empirically, for example by logging the Kalshi `cfbenchmarks_value_5hz` feed against exchange websockets.
- The exact exponential-weighting parameter (lambda) and the dynamic spacing and size-cap formulas exist in the methodology PDF, but I did not extract numeric values.

## 2. Accessing real-time BRTI (Kalshi passthrough, websockets, CF Benchmarks feeds)

### Takeaway
Kalshi users get BRTI through three channels, all authenticated with Kalshi API keys and gated by account entitlement:
- **REST passthrough** to the CF Benchmarks API: 50 read tokens per call, which is 4 requests/s at the Basic tier.
- **`cfbenchmarks_value` websocket**: about 1 tick/s, and it carries Kalshi-computed trailing 60 s averages and a running **quarter-hour final-minute average**.
- **`cfbenchmarks_value_5hz` websocket**: up to 5 ticks/s for BRTI, ETH, SOL, XRP and DOGE, raw values only.

These feeds make the settlement average directly observable as it forms.

### Cited Findings
- REST passthrough:
  - `GET https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI` forwards to `https://www.cfbenchmarks.com/api/v1/values?id=BRTI`.
  - Requests use standard Kalshi request signing, with the signature computed over the path without the query string.
  - Access is "available only to accounts with the appropriate entitlement".
  - Each call costs **50 tokens** from the Read bucket (default call = 10); at Basic tier's 200 tokens/s that "sustains 4 requests per second".
  - `includeVerification` is stripped.
  - History endpoint: `/cfbenchmarks/history/values?id=BRTI&timespan=HOUR&timestamp=…` returns "tick-level values … at intra-second granularity".
  - Upstream errors map to 404, 429 and 503.

  — [Kalshi docs: CF Benchmarks REST Passthrough](https://docs.kalshi.com/cfbenchmarks/rest-passthrough.md)
- `cfbenchmarks_value` websocket channel:
  - Takes `index_ids` (BTC = `BRTI`); "Ticks are emitted roughly once per second".
  - Each message includes `avg_60s_data`, a trailing 60 s window `[ts-60000, ts)`.
  - It also includes `last_60s_windowed_average_15min`, present **only in the final minute before :00/:15/:30/:45**. Its window is `(close-60000, close]` with "second-indexed counts: :01 -> 1 … :59 -> 59, close tick -> 60".
  - It covers 21 coins.

  — [Kalshi docs: CF Benchmarks Value Feed](https://docs.kalshi.com/websockets/cfbenchmarks-value.md)
- `cfbenchmarks_value_5hz` channel: "up to 5 updates per second" for BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI and DOGEUSD_RTI, described as "the vendor's full publication rate". Messages are "lean raw ticks" without averages. — [Kalshi docs: CF Benchmarks 5Hz Value Feed](https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz.md)
- CF Benchmarks' own page directs anyone who needs "real time or historic data for this index to power a product or service" to a licensing contact. It describes a paid, licensed data service, not a free public feed. — [CF Benchmarks BRTI page](https://www.cfbenchmarks.com/data/indices/BRTI)

### Inferences
- The `last_60s_windowed_average_15min` field effectively publishes the partial settlement sum in real time. Combined with the 5 Hz feed, a bot can compute the locked-in portion of the settlement average exactly, with no proxy error.
- REST at 4 req/s is too slow and too costly for the final-minute race. Use the websockets.
- The Kalshi field counts one sample per second (1…60), while BRTI now prints at 5 Hz. Kalshi therefore appears to take one value per second. This should be verified (see Gaps).
- Access depends on entitlement, so availability to a given retail account is not guaranteed.

### Gaps
- No published end-to-end latency figure for these Kalshi feeds (CF Benchmarks computation → Kalshi websocket) was found. Measure it by comparing `source_ts_ms` with local receive time.
- It is unclear which 200 ms sub-tick per second counts toward the official settlement: the first print at or after each second, or the last before it. The Kalshi docs show second-indexed counts but do not state the rule. Check the KXBTC15M contract terms PDF and the rulebook.
- CF Benchmarks' direct websocket and API pricing for non-licensee retail users was not found.

## 3. Averaging effect: how much the 60 s averages shrink uncertainty, and the "target formation" timing

### Takeaway
Two structural facts:
- **The strike is fully determined at the moment the market opens.** It is the average of the 60 s before open, which is the same window as the previous 15-minute market's settlement average. Nothing about the target is formed after open.
- Averaging over the final minute cuts the standard deviation of the final-minute outcome by about 42% versus a point price at close. Once inside the final minute, uncertainty collapses quickly: with 20 s left, the remaining SD is about 1/7 of the SD of a 20 s point move.

### Cited Findings
- The strike-window and close-window definitions come from the contract rule supplied by the coordinator (Kalshi market data). Kalshi computes `last_60s_windowed_average_15min` for every quarter-hour close, :00/:15/:30/:45. — [Kalshi docs: CF Benchmarks Value Feed](https://docs.kalshi.com/websockets/cfbenchmarks-value.md)
- A third-party bot repo describes KXBTC15M as settling on whether the close average is >= `floor_strike`, "a reference price set from the window's own open", with "a 60-second average of CF Benchmarks' BRTI" as the source. — [GitHub: cashmoney-byte kalshi-btc-15m-bot](https://github.com/cashmoney-byte/Cashmoney--kalshi-btc-15m-bot) (unverified secondary source)
- BRTI is a martingale by construction (no trailing window inside the index itself). — [RTI Methodology §4.2](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)

### Inferences (my own calculations; Brownian-motion approximation, 1 sample/s, BTC = $83,558)
- **Per-second scale.** At 50% annualized volatility, the BRTI 1-second SD is about $7.44 (0.89 bp). At 35% vol it is about $5.21; at 70% vol, about $10.42.
- **Final-minute averaging.**
  - Variance of the mean of 60 future 1 s samples = sigma^2 * n(n+1)(2n+1)/(6*60^2) with n = 60. That is sigma^2 * 20.5, i.e. "effective time" of about 20.5 s instead of 60 s.
  - At 50% vol the SD is $33.7 for the average, versus $57.6 for a point price 60 s ahead (42% less).
  - Across a whole 15-minute window, the settlement behaves like a point price about 860 s ahead instead of 900 s. Whole-window SD is about $219 versus $223, so averaging barely matters early in the window and dominates only near the end.
- **Remaining SD of the settlement average k seconds into the final minute** (50% vol): k=0 $33.7; 10 $25.7; 20 $18.5; 30 $12.1; 40 $6.6; 50 $2.4; 55 $0.92; 58 $0.28; 59 $0.12. Compare a naive point-price model with the same time left: 40 s left $47; 20 s left $33; 10 s left $23.5. **A market maker who prices the final minute off spot volatility, ignoring the locked-in average, overstates uncertainty 3–10x in the last 20 s.** That would push quotes toward 50¢ when the outcome is near-certain. This is the main averaging-mispricing hypothesis to test.
- **Drift inside the final minute.** The expected settlement is not the current spot. It is E[A] = (sum of samples so far + (60-k) * current spot)/60. If spot moves sharply at :50, only about 10/60 of the move passes into the average. Traders who react to spot alone will overreact.
- **At open.** The spot price at the moment of open differs from the strike by roughly N(0, $33.7) at 50% vol, because the strike lags as a 60 s average. Using an effective 860 s horizon, the fair P(Yes) at open is:

  | Spot minus strike | Fair P(Yes) |
  |---|---|
  | +$10 | 0.518 |
  | +$25 | 0.546 |
  | +$50 | 0.591 |
  | +$100 | 0.677 |

  A market that opens at about 50¢ regardless of where spot sits relative to the strike is mispriced by up to several cents. This is the "open-minute target formation" edge. It is really a "strike is stale vs spot" edge: the strike is known before open, and spot is already elsewhere.
- **Strike forecasting.** During the last minute of market N-1, the strike for market N is forming in exactly the same way, so the same math applies for anyone quoting market N before its open.
- **Caveats on all numbers.** They assume Gaussian increments with no microstructure noise. Real BTC has fat tails and volatility clustering, and BRTI's depth weighting smooths the 1 s noise, so the effective 1 s SD may be a bit lower than spot top-of-book. If Kalshi samples at 5 Hz instead of 1 Hz, the figures change slightly.

### Gaps
- I found no published empirical study of whether Kalshi KXBTC15M quotes misprice the final-minute average or the open-time spot-vs-strike gap. Evidence would need to come from logging Kalshi order books together with the CF websocket.
- I did not find out whether KXBTC15M markets are listed and tradeable before their open time, which would make the strike-formation minute tradeable.

## 4. Lead-lag across BTC venues (sub-second to 1 minute)

### Takeaway
High-frequency evidence finds CME futures and Binance (spot and perps) at the front of price discovery, with lead times measured in **tens to hundreds of milliseconds**. Older 1-second-sampled work (2021) found CME leading Coinbase and Binance by about 7–8 s, but that is likely a methodological artifact. BRTI's inputs are USD spot venues (Coinbase, Kraken, etc.) that generally follow Binance and CME. So **Binance perps/spot and CME should lead BRTI by roughly 50–500 ms, plus BRTI's own 200 ms calculation cadence and distribution delay.** No paper measures BRTI's lag directly.

### Cited Findings
- arXiv 2506.08718 (2025) used Hayashi–Yoshida on tick data for CME BTC futures vs Binance BTC spot, across five 2024 event days. Results:

  | Event day | Lead time | LLR | Direction |
  |---|---|---|---|
  | 1 | 0.055 s | 1.11 | CME leads |
  | 2 | 0.066 s | 1.15 | CME leads |
  | 3 | 0.063 s | 1.36 | CME leads |
  | 4 | -0.057 s | 0.97 | Binance spot leads |
  | 5 | 0.15 s | 1.94 | CME leads |

  The paper's summary: "futures markets … tend to lead overall, [but] high-volatility periods produce mixed outcomes". — [arXiv 2506.08718](https://arxiv.org/pdf/2506.08718) (master's-thesis-style document, not peer reviewed). The search summary attributed the spot-leading case to 5 Aug 2024, but I did not confirm that date mapping in the text.
- Cosenza & Stalder (SSRN 4983566) use high-frequency spot and perpetual data and conclude that Binance's spot and perpetual markets are the primary source of BTC price discovery, with lower transaction costs and higher volume strengthening that role. — [SSRN 4983566](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4983566) (abstract page blocked to curl; finding taken from the search-result summary, so verify)
- A 2025 Journal of International Money and Finance paper finds CME futures leading BTC spot, with trade size a key factor. — [IDEAS/RePEc: Price discovery in bitcoin spot and futures markets](https://ideas.repec.org/a/eee/jimfin/v159y2025ics0261560625001500.html) (from the search summary, not read in full)
- Bitwise (2021) found CME futures leading Binance by about 7.3 s and Coinbase by about 8.4 s. This is **old (2021) and uses coarser methods**, which conflicts with the millisecond-scale estimates above. — [Bitwise white paper (SEC exhibit)](https://www.sec.gov/files/rules/sro/nysearca/2021/34-93445-ex3a.pdf)
- A practitioner blog (Arrakis, not peer reviewed) applied Hayashi–Yoshida to 29 perpetual markets. Binance USDⓈ-M perps led Hyperliquid in 29/29 assets; Lighter led Hyperliquid in 27/29; Binance led Lighter in 23/29. — reported via [ChainCatcher](https://www.chaincatcher.com/en/article/2269446) (secondary)

### Inferences
- Plausible order of venues: Binance perps ≈ CME futures (when CME is open) → Binance spot → Coinbase/US spot → BRTI (+0–200 ms calculation cadence, plus distribution latency) → Kalshi quotes (seconds; see section 5).
- CME is closed weekends and daily 5–6 pm ET. During those hours Binance perps are the clear leader.
- At the 15-minute horizon the lead itself (<1 s) is tiny in price terms: about $7/√s implies a few dollars at most. It matters only (a) in the final ~10 s of the averaging window and (b) in picking off stale Kalshi quotes after sharp moves.

### Gaps
- No study measures BRTI's lag versus Binance or Coinbase directly. This needs measuring by timestamping the 5 Hz feed against exchange websockets.
- No peer-reviewed millisecond study compares Binance perps with Coinbase spot specifically for 2025–2026.

## 5. Kalshi / Polymarket quote reaction speed and latency-arbitrage evidence

### Takeaway
Latency arbitrage on 15-minute and 5-minute crypto up/down markets is widely discussed and was material enough that **Polymarket added taker fees to its 15-minute crypto markets in January 2026, explicitly to neutralise latency arbitrage.** For Kalshi, the only lag figure I found is an unmeasured claim of **3–7 s** repricing lag in a bot repo's README. Claimed Polymarket lags range from 2–10 s to 30–90 s. None of these figures is independently verified.

### Cited Findings
- Polymarket added a taker-only fee to 15-minute crypto markets. The fee is highest at 50% probability and falls toward 0 and 100%, and proceeds fund daily USDC maker rebates. The stated aim was to neutralise latency-based arbitrage that had emerged under the zero-fee structure. — [The Block (Jan 2026)](https://www.theblock.co/post/384461/polymarket-adds-taker-fees-to-15-minute-crypto-markets-to-fund-liquidity-rebates); [Unchained](https://unchainedcrypto.com/polymarket-introduces-taker-fees-in-15-minute-markets/)
- Sources disagree on the peak fee: about 1.56% ([DEV Community post](https://dev.to/lkto1m/february-2026-changed-polymarket-forever-heres-what-happened-to-my-bots-numbers-2fi5)), about 1.80% ([KuCoin guide](https://www.kucoin.com/blog/ph-polymarket-fees-trading-guide-2026)), and up to 3% ([Unchained](https://unchainedcrypto.com/polymarket-introduces-taker-fees-in-15-minute-markets/)). The DEV author says a 1.56% taker fee "eliminates [profit] entirely on thin margins" and that they moved to maker orders.
- Kalshi lag claim, from the kalshibot repo README (unverified): "Binance spot BTC updates via WebSocket approximately every 200ms"; "Kalshi contract prices reprice with a 3-7 second lag relative to spot movements". The README attributes this to a thinner order book and to market makers lacking low-latency infrastructure. — [GitHub: brandononchain/kalshibot](https://github.com/brandononchain/kalshibot)
- Polymarket lag claims:
  - A GitHub bot says Polymarket's order book lags Binance by 2–10 s and triggers on moves of more than 0.4% in 30 s. — [GitHub: learningworship/polymarket-latency-bot](https://github.com/learningworship/polymarket-latency-bot) (README not retrievable via raw URL; figure from the search summary)
  - Another blog claims 30–90 s lag on 5-minute markets. — [chudi.dev](https://chudi.dev/blog/how-i-built-polymarket-trading-bot) (69.6% win rate on only 23 trades, so not meaningful)
  - Lag estimates differ by about 10x across sources.
- A widely repeated media claim that a bot turned $313 into $414k in a month is unverified. — [Finance Magnates](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/); [Yahoo Finance](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- TurbineFi (a vendor) compiled 5,732 public backtests of Kalshi BTC 15-minute bots: 2,154 positive simulated P&L and 2,496 negative. — [TurbineFi](https://www.turbinefi.com/blog/5732-backtests-kalshi-bitcoin-15-minute-bots-2026) (vendor-curated, simulated results)
- Academic context: Kalshi trades are maker-offered and taker-accepted. Makers earn higher returns than takers, and prices show a favorite–longshot bias in which low-price contracts underperform. — Bürgi, Deng & Whelan, "Makers and Takers: The Economics of the Kalshi Prediction Market" (UCD, Jan 2026) (PDF reviewed locally; no URL captured)

### Inferences
- If Kalshi KXBTC15M quotes really lag spot by several seconds, the pickable edge after a move of Δ$ is about φ(z)·Δ/σ_remaining in probability terms. Mid-window, with σ_remaining ≈ $150–220, a sharp $50 move shifts fair value by about 9–13¢. That is large relative to Kalshi taker fees if quotes do not update.
- The maker-favoring evidence and Polymarket's anti-latency-arb fee both suggest that pure taker latency arbitrage is now competitive and fee-constrained.
- The more durable edges are probably model-based:
  - correct handling of the final-minute averaging (section 3);
  - the spot-vs-strike gap at open;
  - quoting as a maker with a fast BRTI and Binance feed, so as not to be picked off.
- Polymarket's 15-minute markets settle on a different oracle (reportedly Chainlink, not verified here). Kalshi–Polymarket "arb" on the same window carries settlement-source basis risk.

### Gaps
- I found no rigorous, independent measurement of Kalshi KXBTC15M quote-update latency after BTC moves, and no academic paper on latency arbitrage in Kalshi or Polymarket crypto up/down markets.
- Kalshi's taker fee for KXBTC15M was not verified in this pass (presumably covered by another researcher).
- The Polymarket peak fee figures conflict (1.56%, 1.80%, 3%). Polymarket's own docs were not checked.

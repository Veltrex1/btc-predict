# Stop taking, start pricing the settlement average

The notes and our own replays point to the same answer: **on KXBTC15M, an automated strategy can only plausibly profit after fees if it stops paying the taker fee and spread on ordinary signals, or if it trades on information the market demonstrably prices wrong.** The best candidates for the second are the exactly computable final-minute settlement average, the strike being stale against spot at the open, and a cross-venue gap with Polymarket US, which settles on the same index. Our measurements explain why the first two attempts failed. A calibrated model that is 78.9% accurate on locked calls carries no information beyond Kalshi's own favourite, which is 79.3% accurate. Buying such a model's calls at the ask therefore pays the whole spread plus a fee of up to 1.75¢, and loses about 2.1¢ per contract. The swing strategy paid the same toll on every round trip. Independent evidence agrees: a vendor study of 6,298 settled KXBTC15M windows found that buying the leading side earns only **+0.67¢ gross against an average 1.55¢ taker fee** ([Bot for Kalshi](https://www.botforkalshi.com/blog/kalshi-bitcoin-bot)), and short-horizon Kalshi crypto prices are essentially calibrated, with a recalibration slope of 0.99 in the final hour ([arXiv 2602.19520](https://arxiv.org/abs/2602.19520)). Makers on KXBTC15M pay **no fee at all**, but they face two designated liquidity providers and adverse selection. So the ranking below favours edges that are either structural or execute passively, and every one must first survive a cheap measurement on the recorder data we already collect. None of the candidates has public evidence of live, after-fee profit. Treat the list as a test plan, not a set of proven strategies.

## Kalshi's fee wall punishes takers and leaves makers untaxed

KXBTC15M is a `quadratic` fee series with multiplier 1. A live check of the API on 2026-10-12 showed no past or scheduled fee changes ([Kalshi API /series/KXBTC15M](https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M); [fee_changes](https://api.elections.kalshi.com/trade-api/v2/series/fee_changes?series_ticker=KXBTC15M&show_historical=true)). Under Kalshi's spec, only `quadratic_with_maker_fees` series charge resting orders ([Kalshi API docs](https://docs.kalshi.com/api-reference/market/get-series.md)). The taker fee is 0.07·P(1−P), rounded up: **1.75¢ at 50¢, 1.47¢ at 30/70¢, and about 0.33¢ at 95¢.** Fills from resting orders are free. A taker at 50¢ therefore needs roughly 2.25¢ of edge once the half-spread is included. That is almost exactly the 2.1¢ our calibrated model lost per contract, which tells us the model's directional edge over the market was roughly zero.

The broader Kalshi evidence points the same way. Across Kalshi markets from 2021 to 2025, **makers averaged −9.64% against −31.46% for takers** ([Bürgi, Deng & Whelan](https://www.karlwhelan.com/Papers/Kalshi.pdf)). That sample excludes the hourly crypto markets, so it supports the direction of the argument, not a specific size.

Being a maker does not come with subsidies. A scan of roughly 17,000 Kalshi incentive programs found **no Liquidity Incentive Program or volume program on any KXBTC15M market** ([Kalshi API incentive_programs](https://api.elections.kalshi.com/trade-api/v2/incentive_programs)). Kalshi lists **two contracted designated providers for "Crypto 15min markets" through 12/31/26** ([Kalshi Help: Liquidity Provider Program](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program)). A retail maker joins the back of their queue, and gets filled mainly when the touch is about to be run over.

The evidence from Polymarket reinforces this picture. Polymarket introduced the same 0.07·p(1−p) taker fee on its 15-minute crypto markets in January 2026, specifically to neutralise latency arbitrage ([The Block](https://www.theblock.co/post/384461/polymarket-adds-taker-fees-to-15-minute-crypto-markets-to-fund-liquidity-rebates)). One developer's 176 trades after the fee netted about $2. Late momentum and late-snipe strategies were net negative despite backtest win rates above 95% ([dev.to manja316](https://dev.to/manja316/176-trades-on-polymarket-what-my-bot-actually-made-its-not-what-you-think-3iib)). An open-source KXBTC15M RSI bot lost $2,386 over 21 days on modelled fills, of which about $715 was fees ([GitHub: kalshi-btc15m-bot](https://github.com/nepalanurag/kalshi-btc15m-bot)).

The pattern is consistent: signals that only re-derive what the book already says lose the toll.

## Ranked candidate edges

The ranking weighs structural plausibility against how cheaply the edge can be falsified with the recorder we already have. The expected sizes are my estimates from the notes' models. They are hypotheses, not measurements.

| Rank | Edge | Evidence strength | Expected size (if real) | Data needed | Decisive test |
|---|---|---|---|---|---|
| 1 | Final-minute settlement-average pricing (exact locked-in sum) | Strong mechanism, no empirical study | Several ¢ on mispriced contracts in the last ~30 s. Fee ≤0.6¢ at 10/90¢. Capacity is limited by depth | Kalshi `cfbenchmarks_value` (running quarter-hour average) and 5 Hz BRTI, book deltas with receive timestamps | Price-conditional calibration of the bid/ask at T−60…T−1 s against the exact conditional probability; simulated takes at the ask with fee and depth |
| 2 | Stale strike vs spot at open, and strike forecasting before open | Strong mechanism, no empirical study | Fair value is 0.546 at +$25 and 0.591 at +$50 vs ~50¢. A few ¢ in the first minute | BRTI 5 Hz, constituent spot, first-minute Kalshi book, market listing times | Regress opening mid on (spot − strike)/σ. Compute the after-fee P&L of taking when the gap exceeds fee + half-spread |
| 3 | Model-informed passive quoting (our model as a maker, not a taker) | Moderate: maker/taker return gap, zero maker fee, 0.67¢ gross leading-side edge | 0–1.5¢ per fill before adverse selection. The sign is unknown | 1 s book plus trade prints (have), queue position proxy, ms timestamps | Markout analysis of fills at our quote price at 1/5/30 s and at settlement; queue-aware replay of the existing model at the bid |
| 4 | Kalshi vs Polymarket US cross-venue gap (identical BRTI settlement) | Moderate: settlement identity confirmed 24/24 windows; no gap statistics | Needs ≥~3.5¢ combined gap taker-taker near 50¢; less with one maker leg | Polymarket US books (slugs built from the clock), Kalshi book, both fee schedules | Record both venues; histogram of executable ask(YES)+ask(NO) after fees, its duration, and depth |
| 5 | Latency: BRTI constituents/Deribit lead Kalshi quotes | Weak: 3–7 s Kalshi lag is an unverified claim; journalists saw repricing "within seconds" | 9–13¢ fair-value shift on a $50 move mid-window, if quotes are stale | ms-timestamped Coinbase/Kraken/Bitstamp/Gemini, BRTI 5 Hz, Kalshi deltas, AWS us-east-1 host | Event study: after constituent moves > k·σ in ≤2 s, measure time until Kalshi best ask crosses fair, and after-fee pick-off P&L |
| 6 | Volatility-regime mispricing (implied vs realized σ near \|d\|≈1) | Weak: regime effects documented; no study at 15 min | ≤1–3¢ in the 60–90¢ late-window zone | Kalshi mid, BRTI, DVOL, macro calendar, time-of-day | Back out Kalshi implied σ per snapshot; test whether realized − implied σ is predictable from DVOL, macro times, weekend flag |
| 7 | 15-minute taker-flow reversal tilt | Moderate academically; tiny | ~1.3 bp gross, well under 1¢ per contract | Taker-buy volume from US venues (Binance blocked) | Use only as a maker skew feature; ablation in the rank-3 replay |
| 8 | Intra-Kalshi YES+NO < $1 | Weak; fee-killed for takers | ~0 after 3.5¢ of fees near 50¢ | Book (have) | Count crossed-sum events in the 1 s book; expected to be near zero |
| 9 | KXCRYPTOLEAD15M LIP (adjacent series) | One unverified user report | $20 pool per window; one user earned $20.30 vs $36.06 losses | Incentive API, that series' book | Out of scope for KXBTC15M; side project only |

The ranking deliberately excludes several ideas, for reasons given in the closing sections: the favourite-longshot bias, funding/open-interest/liquidation signals, options-implied fair value, Polymarket international arbitrage, and settlement manipulation.

## Settlement mechanics create the only structural mispricing candidates

The two top-ranked edges come from one fact. **KXBTC15M does not settle on a price.** It settles on whether the mean of 60 one-second CF Benchmarks BRTI values in the final minute is at or above the same 60-second average before the open ([predictionmarketspicks: KXBTC15M settlement](https://predictionmarketspicks.com/how-it-settles/KXBTC15M)). Kalshi publishes the forming average itself. Its `cfbenchmarks_value` websocket carries `last_60s_windowed_average_15min` during the final minute before each quarter-hour, with second-indexed counts from 1 to 60. A separate channel streams raw BRTI at **up to 5 Hz** ([Kalshi docs: CF Benchmarks Value Feed](https://docs.kalshi.com/websockets/cfbenchmarks-value.md); [5 Hz feed](https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz.md)). Since May 2026, BRTI is computed every 200 ms from seven USD spot venues: Bitstamp, Coinbase, Kraken, Gemini, LMAX Digital, Bullish and Crypto.com. **Binance, Bybit and all perpetuals are not inputs** ([CME CF Constituent Exchanges](https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf); [RTI Methodology](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)). The US-IP block on Binance therefore costs us less than it seems: our recorder already captures four of the seven constituents.

### Final-minute averaging (rank 1)

With k seconds of the final minute elapsed, the expected settlement value is (sum of samples so far + (60−k)·spot)/60. Under a Brownian approximation at 50% annualised volatility, the remaining SD of the settlement average falls from $33.7 at the start of the final minute, to $12.1 with 30 s left, to $2.4 with 10 s left. A model that uses the point price instead puts it at $47 with 40 s left and $23.5 with 10 s left. **A quoter who ignores the locked-in average overstates uncertainty 3–10× in the last 20 seconds.** That would hold prices too close to 50¢ when the outcome is nearly settled. The same quoter would also overreact to late spot jumps, since only (60−k)/60 of such a jump reaches the average.

These figures are my own calculations from the notes. Real BTC has fat tails and BRTI smooths noise, so they are a starting point, not truth.

Journalistic data on 1,600 windows from 4 September to 11 October 2026 is consistent with prices that are sometimes too confident near the line. Among windows finishing within $50 of the line, 13%–32% flipped in the final two minutes, and in many flips the losing side had been priced at 70¢ or more with two minutes left. Only 7 of 821 windows finishing $50 or more from the line flipped ([OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/are-kalshi-15-minute-bitcoin-markets-rigged)). That study is not price-conditional, which is exactly the gap our data can close.

Two features make this the top candidate. The fee is cheap where the edge would live, at about 0.33¢ at 95¢. And our 1 s book recorder plus the CF channel give an exact, proxy-free fair value. The obvious risk is that the designated providers already do this math. Our own favourite-accuracy figure fits that explanation too.

### Stale strike at open (rank 2)

The strike is the previous market's settlement average. It is fully fixed when the market opens, while spot has already drifted away from it. Using an effective 860-second horizon, fair P(Yes) is **0.518 at +$10, 0.546 at +$25, 0.591 at +$50 and 0.677 at +$100** of spot over the strike (my calculation, same assumptions). If the opening book sits near 50¢ regardless, the first seconds of each window offer edges that exceed the 1.75¢ taker fee. The notes could not determine whether KXBTC15M lists before its open, so they could not say whether the strike-formation minute can be traded directly.

### Execution prerequisites for both

Both edges need the **Kalshi CF websocket channels added to the recorder.** Access to them is entitlement-gated ([Kalshi docs: REST passthrough](https://docs.kalshi.com/cfbenchmarks/rest-passthrough.md)), so check our account first. Two settlement details also need verification against the contract terms: which 200 ms sub-tick within each second counts toward settlement, and whether the strike equals the previous close exactly.

## Passive execution turns a priced-in model into a candidate

Rank 3 reuses what we already built. Our model matches Kalshi's favourite, so its value is not in direction. Its value is as a **fair-value anchor for resting orders** that collect the spread instead of paying it and pay no fee. The arithmetic is direct. The 2.1¢ per contract we lost buying at the ask is roughly the half-spread plus the fee. The same calls filled at the bid would recover about that much, minus adverse selection. Adverse selection is the open question.

Fills cluster exactly when fair value has moved. Near expiry the binary's delta to BTC is enormous, and fast takers sweep stale resting bids. One user of the adjacent LIP described the problem as "the fill risk is the hard part" ([GitHub: lip-ledger](https://github.com/mirabilis-agent/lip-ledger)).

The tooling for defensive quoting exists in Kalshi's API:
- `post_only` orders and amend/decrease endpoints
- order groups, which auto-cancel once a contract limit is filled within a rolling 15-second window and so work as a sweep kill switch
- a self-serve upgrade from Basic (100 write tokens per second, about 10 creates per second) to Advanced (300 per second)

Sources: [Kalshi docs: Order Groups](https://docs.kalshi.com/getting_started/order_groups.md); [Rate Limits](https://docs.kalshi.com/getting_started/rate_limits.md).

A 2021 study found a passive maker strategy beat a naive benchmark in live BTC trading, while the profitability of taker strategies depended on the fee tier ([arXiv 2108.09750](https://arxiv.org/abs/2108.09750)). That is the right template. Quote only when the model's fair value sits at least an estimated adverse-selection margin inside our price. Pull quotes in the final seconds unless rank 1 says the outcome is locked. Skew quotes with weak features such as the 15-minute taker-flow reversal, whose gross edge is only about 1.3 bp ([arXiv 2608.21888](https://arxiv.org/abs/2608.21888)).

The test is a **markout study** on our recorded trades. For every print at a price where we would have been resting, measure the mid move at 1 s, 5 s and 30 s, and the settlement outcome. Assume we were filled only when prints trade through our level, which is a conservative stand-in for queue position. If the average markout at settlement is worse than the half-spread captured, the edge is dead before any live capital is risked.

## Cross-venue and latency edges hinge on measurements nobody has published

Rank 4 is the only true arbitrage candidate. **Polymarket US launched a 15-minute BTC up/down product around 22 September 2026 that settles on BRTI's 60-observation final-minute average at both open and close** ([Arabian Post](https://thearabianpost.com/polymarket-adds-15-minute-bitcoin-markets-to-us-app/)). An open-source recorder matched price-to-beat, settlement and outcome with KXBTC15M on 24 of 24 windows ([housedge PR #171](https://github.com/albatrossbird/housedge/pull/171)). Polymarket US traded **$91.76M in its first full week, rising from $2.40M to $27.71M per day** ([DeFi Rate](https://defirate.com/news/polymarket-us-15-minute-bitcoin-volume-hits-92m-in-first-full-week/)). Flow on a new venue that is heavy in retail is where transient gaps are most likely.

The hurdle is about 3.5¢ of combined gap near 50¢ when taking both legs. One bot design deducts latency and unwind haircuts and caps one-sided-fill losses, which implies leg failures are frequent ([NotACheeto/BTC15ARB](https://github.com/NotACheeto/BTC15ARB)). Polymarket US's fee schedule was not confirmed from a primary source. Its markets also "appear in no listing", so a recorder must build slugs from the clock.

Polymarket international is a different case. It settles on a Chainlink 60-second TWAP since 7 August 2026 ([Gamma API](https://gamma-api.polymarket.com/events?slug=btc-updown-15m-1791770400)), which creates settlement basis risk exactly where books sit near 50¢. It is also geo-restricted for US users. It does not qualify.

Rank 5, latency, has the largest theoretical size and the weakest evidence. The only Kalshi lag figure is an unmeasured **3–7 seconds** in a bot README ([brandononchain/kalshibot](https://github.com/brandononchain/kalshibot)). Against it, OddsShopper observed Kalshi repricing within seconds of large Coinbase moves, and a median window trades 1.9–2.9M contracts ([OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/are-kalshi-15-minute-bitcoin-markets-rigged)). High-frequency studies put leads between BTC venues at **tens to hundreds of milliseconds**, for example CME ahead of Binance spot by 55–150 ms ([arXiv 2506.08718](https://arxiv.org/pdf/2506.08718)). A retail bot running over REST/WS at Basic tier will usually lose that race to contracted providers.

Two edges cannot be tested with the recorder as it stands. Latency needs millisecond-timestamped order-book deltas; the current 1 s snapshots hide it entirely. The cross-venue arbitrage needs Polymarket US books, which the recorder does not yet capture. Rank 6 is cheap to add as a by-product of the others. For each snapshot, back out Kalshi's implied σ and check whether realized-minus-implied is predictable from DVOL, macro release times or the quarter-hour volatility bursts documented on perpetuals ([arXiv 2607.09426](https://arxiv.org/abs/2607.09426)). A volatility error matters most when |d|≈1, the 60–90¢ late-window zone. At the money it barely matters.

## Popular edges that the evidence already rules out

The favourite-longshot bias does not transfer. Kalshi's strong bias, where contracts at 10¢ or less lose more than 60%, comes from markets open at least 24 hours and explicitly excludes hourly crypto ([Bürgi et al.](https://www.karlwhelan.com/Papers/Kalshi.pdf)). Kalshi crypto in the final hour is calibrated, with a slope of 0.99 ([arXiv 2602.19520](https://arxiv.org/abs/2602.19520)), and our own measurement agrees: the favourite is right 79.3% of the time.

Pre-open skews on Polymarket's 15-minute BTC markets are calibrated within about 1¢ across 23,700 markets ([Tsybko RESULTS.md](https://github.com/supervik/polymarket-btc-preopen/blob/main/RESULTS.md)).

The notes found **no out-of-sample study showing that funding, open interest, liquidations or options skew predict 1–15 minute BTC direction.** No listed option expires within 15 minutes, so Deribit can serve only as a volatility input ([PredictionMarketsPicks](https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-markets)).

Last-tick manipulation, documented on Polymarket's 5-minute snapshot markets ([ForkLog](https://forklog.com/en/news/researchers-find-signs-of-manipulation-in-polymarket-five-minute-bitcoin-contracts)), does not apply: KXBTC15M has always averaged over 60 seconds, and it would breach Kalshi's manipulation rules anyway.

## Conclusion

Our two failures were not bad modelling. They were the correct result of paying about 2¢ to act on information the market already had. The useful shift is to treat the 78.9%-accurate model as a pricing engine rather than a signal generator. Pair it with the one input that is both exact and possibly under-used, the forming settlement average. Only take liquidity where that math, or a same-index gap with Polymarket US, opens a gap larger than the fee.

This ordering also sets the engineering priorities:
1. Add Kalshi's CF Benchmarks channels to the recorder.
2. Add millisecond-timestamped order-book deltas.
3. Add Polymarket US books.
4. Run the four falsification studies: final-minute calibration, opening-gap regression, maker markouts, and cross-venue gap histograms.

Each study needs only a few weeks of recordings. Each can kill its edge cheaply, which matters more than finding a winner quickly.

If all four fail, the evidence says KXBTC15M is efficiently priced by its two contracted providers for anyone without their speed and fee terms. In that case the remaining options are the institutional designated-provider route or a different market.

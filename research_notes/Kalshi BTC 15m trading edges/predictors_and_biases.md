# Short-horizon BTC direction predictors and behavioral biases in 15-minute crypto binary markets

Scope note: research done 2026-10-12. WebFetch could not resolve DNS in this session, so primary sources were pulled with curl and read directly (arXiv abstracts, the Bürgi/Deng/Whelan PDF, the Tsybko GitHub RESULTS.md, the arXiv 2602.19520 PDF). The tool budget limited depth on funding, liquidations, options and vol; those gaps are listed explicitly.

## Q1. Evidence for 1–15 minute BTC return predictability (order book, trade flow, funding/basis, liquidations, OI, options, cross-asset, time-of-day, macro)

### Takeaway
Microstructure signals (order-book imbalance, cross-venue trade flow) have real explanatory power only at sub-second to seconds horizons (R² of 10–37% at 500 ms), and it decays fast. At the 15-minute horizon the best-documented effect is a weak sign-based mean reversion after taker-flow-driven candles (AUC about 0.54, gross edge about 1.3 bp against about 5 bp spot costs). It is real but small, and it does not clear spot trading costs. I found no rigorous 2020–2026 study showing funding, OI, liquidations or options skew predicting 1–15 minute BTC direction out of sample.

### Cited Findings
**Order book and trade flow (sub-second to seconds)**
- Linear models built from order-book and trade features across the most liquid BTC venues explain **10%–37% of the variance of 500 ms future returns**, depending on the target venue. Fee regime decides which venue leads and which lags. Taker-strategy PnL after costs depends heavily on the fee tier. A passive maker strategy beat a naive benchmark in live trading with about $1.5M notional. (2021 study.) — [Fragmentation, Price Formation, and Cross-Impact in Bitcoin Markets, arXiv 2108.09750](https://arxiv.org/abs/2108.09750)
- On Bybit BTC/USDT LOB snapshots at 100 ms to multi-second intervals, simple models (logistic regression, XGBoost) with good preprocessing (Kalman / Savitzky-Golay filtering) and engineered depth-imbalance features match or beat DeepLOB and Conv1D+LSTM out of sample. Feature quality matters more than model depth. — [Wang 2025, arXiv 2506.05764](https://arxiv.org/abs/2506.05764)
- A statistical-physics model built on L3 order-book data ("active depth", kinetic energy and momentum of the book) is reported to outperform benchmarks for volatility and expected-return forecasts. A search summary cites an OFI regression R² of about 0.094 for BTC at 1–10 s horizons; I did not verify that number in the full text. — [Li et al., arXiv 2308.14235](https://arxiv.org/abs/2308.14235)
- An equity study puts the most accurate imbalance-forecast horizons at 3–10 seconds. This is equity evidence, cited in a practitioner write-up on crypto imbalance. — [Towards Data Science: price impact of order book imbalance in crypto](https://towardsdatascience.com/price-impact-of-order-book-imbalance-in-cryptocurrency-markets-bf39695246f6)

**15-minute horizon: mean reversion conditioned on taker flow (most directly relevant)**
- At 15-minute horizons, **90% of 183 Binance pairs show significant directional reversal**, against 2.7% of US stocks and ETFs, in every coin-year since 2021, strictly out of sample.
- The signal is in signs, not magnitudes. Lag-1 return autocorrelation is near zero on the major coins, yet simply betting against the previous candle captures most of the effect.
- The reversal concentrates after moves driven by aggressive taker flow (Binance kline taker-buy volume) and grows with flow intensity. How much book depth the move consumed does not condition it.
- **The gross edge peaks near 1.3 bp per trade against a 5 bp round-trip cost**, so it is too small to clear spot costs.
- AUC gap versus equities is +0.031 as designed and +0.011 (CI +0.008 to +0.014) under the most conservative accounting. A search summary reports an absolute out-of-sample AUC of about 0.540.
- Source: [Short-horizon mean reversion in cryptocurrency markets, arXiv 2608.21888](https://arxiv.org/abs/2608.21888)

**Clock-time and time-of-day effects**
- Binance perpetuals show periodic bursts in volatility and volume at 1-minute, 5-minute and **quarter-hour marks**, linked to algorithmic participation (trade-size roundness drops during the bursts).
- Returns at quarter-hour openings are predictable out of sample.
- Opening order imbalance predicts returns over **4–12 hours**, with "much weaker effects at finer clock-time frequencies".
- Source: [The Quarter-Hour Effect, arXiv 2607.09426](https://arxiv.org/abs/2607.09426)

**Macro news**
- NY Fed high-frequency study (30-minute windows around announcements): BTC is largely orthogonal to macro and monetary news. Surprises in the current fed funds target have no impact on BTC. A hawkish surprise in the expected *future* policy path has a much larger impact on BTC than on other assets. (Pre-2023 sample.) — [Benigno & Rosa, NY Fed Staff Report 1052](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr1052.pdf)
- A newer Fed Board study (Kroner, Mohammed & Vega, 2026), which I saw only in a secondary summary: volatility, volume and spreads rise significantly around FOMC, CPI and NFP. Surprises that raise yields or signal risk-off produce substantial BTC and ETH declines. Reactions before 2020 were limited. — [summary via RWA Times Substack](https://rwatimes.substack.com/p/how-do-cryptocurrencies-price-economic); [Liberty Street Economics on the BTC macro disconnect](https://libertystreeteconomics.newyorkfed.org/2023/02/is-there-a-bitcoin-macro-disconnect/)

**Funding, liquidations, OI**
- The search found no rigorous intraday out-of-sample study. The available sources are commentary on mechanics and anecdotes. For example, negative funding coexisted with mostly short liquidations in March 2025, so the two can send opposite signals. — [CryptoSlate](https://cryptoslate.com/short-liquidations-contradict-negative-funding-rates-in-perpetual-futures/). The only academic hit was a student-journal paper on BitMEX funding and liquidation as a stopping-time problem, not a forecasting study. — [IWU Undergraduate Economic Review](https://digitalcommons.iwu.edu/uer/vol17/iss1/12)

### Inferences
- The project's Brownian model already captures "where price is versus the target". Signals beyond it must forecast the **remaining** path. Order-book and flow signals decay within seconds to a minute, so their value for a 15-minute binary sits mostly in the last 1–2 minutes and in latency (Binance/Coinbase leading the CF Benchmarks BRTI and leading Kalshi's book), not in pre-window direction calls.
- The 15-minute reversal result (2608.21888) applies directly to the "previous candle" question. A 1.3 bp gross edge on a roughly 0.3–0.5% 15-minute BTC sigma shifts the up probability by only a fraction of a percentage point, likely well under 1¢ on a 50¢ contract. That is below Kalshi's taker fee of about 1.75¢ at 50¢ (see Q3). It is usable only as a maker-side tilt, if at all.
- The quarter-hour effect hints at microstructure noise exactly at Kalshi window boundaries (the :00/:15/:30/:45 opens), which could affect the open reference price. The documented predictability is at the 4–12h horizon, not within 15 minutes.
- Macro releases (8:30 ET CPI/NFP, 14:00 ET FOMC) are best treated as **volatility regime** inputs (widen sigma), not direction inputs, unless one has a real-time surprise feed.

### Gaps
- No 2020–2026 paper found with out-of-sample accuracy or R² for perp funding, basis, OI change or liquidation-cascade features at 1–15 minute BTC horizons.
- No study found on Deribit skew, risk reversals or DVOL changes as 1–15 minute direction predictors.
- No study found on ES-futures or DXY lead-lag versus BTC at minute horizons.
- I could not verify the absolute AUC of 0.540 (from a search summary) or the R² of 0.094 for 2308.14235 in the full texts.
- Cumulative volume delta at 5–15 minute horizons appears only as a conditioning variable (2608.21888), not as a standalone predictor with reported accuracy.

## Q2. Volatility forecasting for 15-minute horizons and its impact on binary fair value

### Takeaway
The literature favors HAR-type realized-vol models with jump and semivariance components over GARCH for BTC, but almost all of it targets 1-day or longer horizons. I found no study on DVOL or implied vol for 15-minute forecasting. Kalshi practitioners note that no listed option expires in 15 minutes, so a 15-minute implied vol must come from a term-structure assumption. Vol matters most for binary fair value when the price is a moderate distance from the strike late in the window.

### Cited Findings
- Across 18 HAR variants (BTC, 2012–2018, an older sample), the best one-day out-of-sample model includes jump and semivariance components (HARQ-F-J). Jumps matter at all horizons, and structural breaks improve forecasts. — [Forecasting the volatility of Bitcoin: the importance of jumps and structural breaks](https://eu.vlex.com/vid/forecasting-the-volatility-of-1049450990)
- HAR models beat GARCH for forecasting realized vol of both BTC and E-mini S&P 500. — [Aganin et al., HSE](https://ej.hse.ru/article/view/29162)
- Tail-event measures added to HAR-RV help most at short and intermediate horizons, especially under asymmetric loss that penalizes under-prediction. — [Gkillas, Gupta, Pierdzioch](https://repository.up.ac.za/items/e77d82a7-dfbd-477a-9f52-23451f8bcef3/full)
- Volatility and volume bursts at 1-, 5- and 15-minute clock marks on Binance perps imply strong intraday seasonality, including at quarter-hour boundaries. — [arXiv 2607.09426](https://arxiv.org/abs/2607.09426)
- Volatility, volume and spreads rise around FOMC, CPI and NFP releases (2020+). — [Kroner/Mohammed/Vega summary](https://rwatimes.substack.com/p/how-do-cryptocurrencies-price-economic)
- Kalshi-focused practitioner note: no listed option expires in fifteen minutes, so there is nothing to back-solve a 15-minute implied vol from. — [predictionmarketspicks.com KXBTC15M explainer](https://predictionmarketspicks.com/articles/kalshi-bitcoin-15-minute-markets)
- Settlement averaging lowers the effective terminal vol. Each KXBTC15M reference price is the mean of 60 one-second CF Benchmarks BRTI values, so late-window spikes are diluted: "45 of the 60 samples were already locked in". — [predictionmarketspicks.com: How Kalshi settles Bitcoin](https://predictionmarketspicks.com/articles/how-kalshi-settles-bitcoin)

### Inferences
- For a digital option, fair value is roughly N(d), with d ≈ ln(S/K)/(σ√τ). Sensitivity to σ is zero at the money (d = 0) and largest when |d| is around 1. For 15-minute markets, a vol error matters most when BTC sits about one sigma-of-remaining-time from the line, which is exactly the 60–90¢ region late in the window.
- Averaging over the final 60 seconds means the effective variance in the last minute is about one third of point-settlement variance (for an arithmetic average of Brownian motion, Var = σ²T/3 over the averaging window). A model that ignores the averaging will **overstate flip probability in the final minute**. This is a modelling inference, not a sourced figure.
- Since the market already prices Brownian probabilities, the edge from better vol comes only where the market's implied sigma differs from realized, for example around macro release times, quarter-hour bursts, or weekend versus weekday regimes.

### Gaps
- No study found on DVOL (Deribit) as a predictor of 15-minute or 1-hour realized vol, or on implied minus realized spreads at sub-hour horizons.
- No quantitative estimate found of how much better vol forecasts change binary PnL for 15-minute contracts.
- No 2020–2026 HAR study found targeting sub-hour BTC horizons; the HAR evidence found is daily and older.

## Q3. Behavioral biases in short-dated crypto up/down markets (Kalshi, Polymarket)

### Takeaway
Direct evidence for BTC 15-minute markets says they are **well calibrated**. Polymarket 15-minute BTC pre-open prices match realized frequencies within about 1¢ across 23,700 markets. On Kalshi KXBTC15M, a vendor study finds buying the leading side gross-positive by only about 0.67¢, below the average 1.55¢ taker fee. Late "flips" come almost entirely from windows finishing within $25–50 of the line. The broad Kalshi favorite-longshot bias (contracts at or below 10¢ lose more than 60% for buyers) comes from longer-dated markets and **explicitly excludes** the hourly crypto markets.

### Cited Findings
**Polymarket BTC up/down, pre-open calibration (Nov 2025–Jul 2026)**
- 23,700 clean 15-minute markets, with order-book mids 1 minute before open.
- Pre-open prices rarely move far from 50¢. 90% sit within 45–55¢ and 99.9% within 40–60¢. The skew forms mostly in the last 5–10 minutes before open: mean |p−0.5| goes from 0.37¢ at 60 minutes to 2.2¢ at 1 minute.
- Calibration: ECE 0.0087, slope 1.15, and 1 of 12 bins (45–46¢, realized 42.5%) off the diagonal. Example: 56.5¢ → 57% Up; 47.4¢ → 46.8% Up.
- 5-minute markets (37,343): ECE 0.0055, slope 0.89. 1-hour markets (8,859): ECE 0.0076, slope 0.99.
- The author's conclusion is that the pre-open skew carries a little information, it is already priced, and fades of it have no edge, especially after fees.
- Sources: [Tsybko, "Does Polymarket Know Which Way BTC Will Move?"](https://viktoriatsybko.substack.com/p/does-polymarket-know-which-way-btc); [RESULTS.md](https://github.com/supervik/polymarket-btc-preopen/blob/main/RESULTS.md)

**Kalshi KXBTC15M, leading-side and fee economics (vendor data; commercial source)**
- Across 6,298 settled windows, buying the leading side had a gross edge of about **+0.67¢ per contract** against an average taker fee of about **1.55¢**.
- The vendor's conclusion is that taking the offer is net negative, and only resting (maker) orders are not structurally negative. A search summary also says Kalshi charges no maker fee on this series; I did not verify that on the vendor page or with Kalshi.
- Source: [Bot for Kalshi blog](https://www.botforkalshi.com/blog/kalshi-bitcoin-bot)

**Kalshi KXBTC15M, near-expiry "late flip" behavior (journalistic; 1,600 windows, Sep 4–Oct 11 2026)**
- 7 of 821 windows that finished $50 or more from the line flipped in the final 2 minutes, under 1%. 166 flips came from windows finishing within $50, and most finished within $25.
- Among windows finishing within $50, the flip rate ran 13%–32% by sample.
- In many flips the losing side was priced at 70¢ or more with 2:00 left: 23 of 31, 13 of 22, 9 of 25 and 10 of 19 across samples.
- Example: Yes was 99.5¢ with 2:00 left and 91¢ with 30 s left, and settled No by $2.81.
- The median finishing margin is about $33–36 on an $80k+ asset, and a median window trades about 1.9–2.9M contracts.
- Kalshi repriced within seconds of large Coinbase moves.
- Source: [OddsShopper, "Kalshi 15-Minute Bitcoin Rigged?"](https://www.oddsshopper.com/articles/prediction-markets/are-kalshi-15-minute-bitcoin-markets-rigged)

**Settlement mechanics**
- The contract settles on whether the 60-second average of BRTI at close is at or above the 60-second average at open. A tie resolves YES. — [predictionmarketspicks.com KXBTC15M settlement](https://predictionmarketspicks.com/how-it-settles/KXBTC15M)

**Latency claim (unverified)**
- An open-source bot claims Kalshi's book takes several seconds to reflect Binance spot moves, but reports no measured results. — [brandononchain/kalshibot](https://github.com/brandononchain/kalshibot)

**Polymarket 15-minute trader profitability**
- One week of on-chain fills (Feb 2–8, 2026; 2,688 markets): most wallets lose, and a few extract hundreds of thousands of dollars. — [Telonex research](https://telonex.io/research/top-crypto-traders-polymarket-15m)

### Inferences
- The OddsShopper data suggest the "90¢ with 2 minutes left" region is where model edges could exist. Those prices may sometimes be too confident when BTC is within about one sigma-of-remaining-time of the line, and too cheap when it is far away (only 7 of 821 wide windows flipped). The sample has no price-conditional calibration, so this is a hypothesis to test on your own Kalshi trade history, not a finding.
- The Polymarket slope of 1.15 at 15 minutes is mildly underconfident (prices too compressed), but within noise. It does not support a tradable pre-open bias.
- Any edge is likely to be maker-side: rest orders at model fair value minus a margin, especially in the last 2–3 minutes. Taker edges need more than about 1.5–1.75¢ of mispricing near 50¢. Kalshi's fee is lower at extreme prices: at 0.07·P(1−P) per contract, a 95¢ contract pays about 0.33¢ before rounding.
- I found no evidence specific to 15-minute crypto markets on weekend or time-of-day retail flow, or on anchoring to the target.

### Gaps
- No peer-reviewed study of Kalshi KXBTC15M or Kalshi hourly crypto calibration. The best data are a commercial vendor study and a journalistic rerun series, neither price-conditional.
- No within-window calibration (for example, price at T−2 minutes versus outcome) published for Polymarket or Kalshi 15-minute BTC markets. The Polymarket study covers only pre-open.
- No data found on weekend or overnight mispricing, or on calibration of cheap (1–10¢) and expensive (90–99¢) contracts specifically in 15-minute crypto markets.

## Q4. Academic literature on Kalshi efficiency/calibration and favorite-longshot bias in prediction markets

### Takeaway
Kalshi shows a strong favorite-longshot bias overall: contracts at or below 10¢ lose more than 60% after fees, and contracts above 70¢ earn small positive returns. The pattern is strongest for takers, and the crypto category has the steepest price-return slope. These results exclude short (hourly) crypto markets. A 353M-trade study finds crypto markets on both Kalshi and Polymarket roughly well calibrated, particularly at short horizons (Kalshi crypto slope 0.99 within the final hour).

### Cited Findings
**Bürgi, Deng & Whelan, "Makers and Takers: The Economics of the Kalshi Prediction Market" (Jan 2026)**
- Sample: Kalshi from 2021 to April 2025, contracts with at least $1,000 volume and a final spread of 20¢ or less. Kalshi's hourly crypto and stock-index markets are excluded; only markets open at least 24 hours are kept.
- Prices become more accurate closer to close, but show a clear favorite-longshot bias. **Contracts at or below 10¢ lose over 60%** post-fee, while **contracts above 70¢ show statistically significant small positive returns**. Example: a 95¢ contract that wins 98% has a pre-fee return of +3.1%.
- Average pre-fee return is −20%. **Makers average −9.64% versus −31.46% for takers.** Makers buying at 50¢ or more earn about +2.6%.
- Fees: taker fee 0.07·P(1−P), rounded up. Over 100 contracts at 50¢ this works out to 1.77%. Kalshi started charging makers after April 2025.
- The price coefficient in the pre-fee profit-on-price regression is **0.058 for Crypto** (8,150 obs) versus 0.034 for All and 0.032 for Financials. The crypto slope is the steepest, meaning the strongest favorite-longshot gradient.
- The bias weakens somewhat in 2025 (coefficient 0.021 versus 0.048 in 2024).
- Sources: [paper PDF](https://www.karlwhelan.com/Papers/Kalshi.pdf); [CEPR DP20631](https://cepr.org/publications/DP20631); [VoxEU column](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)

**"Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets" (arXiv 2602.19520)**
- Data: 353M trades over 429,000 binary contracts on Kalshi and Polymarket.
- **Kalshi crypto logistic recalibration slopes by time-to-resolution: 0.99 (0–1h), 1.01 (1–3h), 1.07, 1.01, 1.01, 1.21 (24–48h), 1.12, 1.09, 1.36 (1mo+).** A slope of 1 means calibrated; above 1 means prices are compressed and favorites underpriced. So short-horizon crypto on Kalshi is essentially calibrated.
- **Polymarket crypto 0–1h slope is 1.753 and 1–3h is 1.390**, versus Kalshi's 0.993 and 1.013, which suggests Polymarket prices near expiry are too compressed. The table marks these bins with a dagger, which I take as a reliability caveat.
- Pooled Polymarket crypto slope is 1.05 (mildly underconfident).
- The trade-size effect for crypto is null (Kalshi Δ(L−S) = −0.02).
- The strongest pattern is political underconfidence; weather markets are overconfident at short horizons (0.69 at 0–1h).
- About half of raw slope variation is estimation noise under event-clustered standard errors.
- Source: [arXiv 2602.19520](https://arxiv.org/abs/2602.19520)

**Polymarket and other markets**
- A Polymarket database paper reports low-priced tokens with negative average payoffs and high-priced tokens with positive payoffs, consistent with the favorite-longshot bias. — [Polymarket-v1 Database, arXiv 2606.04217](https://arxiv.org/pdf/2606.04217)
- A blog claims the opposite direction for 90–100¢ contracts. This conflicts with the academic evidence and is low-reliability. — [simplefunctions.dev](https://simplefunctions.dev/concepts/longshot-bias)
- Kalshi's own reliability study, as reported in the press, says contracts at or below 10¢ lose more than 60% on average. — [Crypto Briefing](https://cryptobriefing.com/kalshi-prediction-markets-reliability-study/)
- InTrade prices were found too high for longshots (Page & Clemen 2013; older), as cited in Bürgi et al. — [Kalshi paper](https://www.karlwhelan.com/Papers/Kalshi.pdf)

### Inferences
- Within the final hour, Kalshi crypto prices look calibrated (slope 0.99), so a general "sell longshots" rule should not be expected to work on KXBTC15M. The documented favorite-longshot bias lives in longer-dated, thinner, taker-heavy markets.
- If the Polymarket 0–1h crypto compression (slope 1.75) is real, cross-venue comparison (Polymarket versus Kalshi on the same 15-minute BTC window) could flag mispricing. The dagger caveat and Polymarket mid-versus-fill issues make this a hypothesis only.
- Maker returns beat taker returns by about 22 percentage points on Kalshi overall, which supports a passive-execution design.

### Gaps
- I could not locate the Reichenbach & Walther (2026) Polymarket calibration paper cited in arXiv 2606.04217.
- No Kalshi-published calibration specifically for the 15-minute or hourly crypto series.
- I did not confirm the current (2026) Kalshi fee schedule for KXBTC15M, including maker fees.

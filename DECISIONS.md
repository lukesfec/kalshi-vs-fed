# Decisions Log

One line each: decision and reason. "Proposed" entries await Luke's sign-off.

## Phase 0
- **Headline series = `KXCPI` (CPI-U MoM, SA).** It's the Kalshi contract that matches the Cleveland Fed's headline CPI nowcast measure exactly.
- **Legacy `CPI-*` events are part of series `KXCPI`.** The API files them there, so no separate `CPI` series pull is needed.
- **Truth = Kalshi `expiration_value`, cross-checked against ALFRED first-release CPI.** The contract settles on the first BLS print, one decimal, and ignores revisions. Final revised FRED values would be the wrong target.
- **Head-to-head sample starts Mar 2022 (≥5 strikes per event).** Events with 1–4 strikes can't form a meaningful probability distribution. *(Default adopted 2026-10-27; Luke can override.)*
- **Exclude Oct 2025 (`KXCPI-25OCT`).** BLS never published that month's CPI, so it settled via Kalshi's fallback rule, not a real print. *(Default adopted.)*
- **Hourly candles for CPI markets.** That's fine enough to align with the ~10:00 ET nowcast update, at 1 request per market. 1-minute needs chunking for little gain. *(Default adopted.)*
- **1-day and 3-day horizons use the last nowcast published before that day (carry-forward), flagged as such.** The archive has no nowcasts for the final 2–3 business days before a release. Carry-forward uses only information available at the time, so there's no lookahead. *(Default adopted.)*

## Phase 1
- **Ingestion writes raw API responses to `data/raw/` and small tidy CSVs to `data/processed/`. Postgres loading waits for Phase 2.** Keeps "download" separate from "transform", so the database can always be rebuilt from cache.
- **Kalshi requests are throttled to ~5/s, with exponential backoff on HTTP 429.** Unauthenticated limits aren't documented. At 5/s we see a 429 roughly every 25 requests, and backoff absorbs it.
- **Candle requests are chunked at 4,800 candles.** The API rejects windows over 5,000 candles.
- **Calibration universe = stratified random sample.** Up to 40 series per Kalshi category (seed 42); combo/parlay series (`KXMVE…`) excluded. From each series, all settled binary markets with volume > 0, capped at 50 per series (seeded). The full universe is too large to pull (15,438 series; one unfiltered historical page covers <1 hour of closes). Sampling whole series at random doesn't depend on outcomes, so it can't bias calibration. The cap stops a few huge series from dominating.
- **Combo/parlay markets (`KXMVE…`) are excluded from calibration.** They're auto-generated multi-leg products with different pricing mechanics, and they'd swamp the sample by count.
- **Daily candles for the calibration set.** We only need prices at fixed times-to-close (e.g. 1, 7, 30 days), and one request covers a market's whole life.
- **First-release CPI comes from ALFRED vintages via a binary search for each month's release date.** A month only appears in vintages on/after its release. This gives both the exact release date and the as-first-printed index levels without needing an API key.
- **First-release MoM is computed within the release-day vintage and rounded half-up to 1 decimal.** That's how BLS computes the printed number (from the index levels published that day, including any February seasonal revisions).
- **CPI history starts Jun 2013.** It matches the start of the Cleveland Fed nowcast archive, giving the long error history Phase 4 needs for expanding-window error variance.
- **The Cleveland Fed's vertical-line release markers are kept separately (`nowcast_release_markers.csv`), not as observations.** They're chart annotations, but they give an independent record of release dates.
- **Truth check passed:** Kalshi `expiration_value` = ALFRED first-release MoM (1dp) in 62/62 comparable events; all 64 events' yes/no results agree with their value. Oct 2025 and Nov 2025 have no official MoM print (shutdown) but Kalshi settled both at 0.2.
- **Release dates are triangulated from ALFRED and the Cleveland Fed's markers, which agree on every month.** Kalshi's `close_time` isn't always the release date. In 2021 contracts closed the evening before release, and Sep 2025 closed on its originally scheduled date (Oct 15) while BLS released on Oct 24.
- **Oct 2025 and Nov 2025 are excluded from the head-to-head (kept in the database, footnoted).** BLS printed no MoM change for either month, so there's no real CPI value to score against; Kalshi's 0.2 came from its fallback rule. *(Proposed 2026-10-27; Luke didn't object.)*
- **Sep 2025 is kept, with both sources cut off at Kalshi's last trade (Oct 15) for horizons that fall after it.** Horizons count from the actual release (Oct 24). The cap stops the nowcast from getting days of information the market never had. *(Proposed; Luke didn't object.)*

## Phase 2
- **Candle `ts` = end of the candle period.** A candle with `ts <= t` contains only information available at t, which makes "no lookahead" a single `<=` condition everywhere.
- **Times: CPI release = 08:30 ET; nowcast visible from 10:00 ET on its date.** BLS publishes at 8:30. The Cleveland Fed says it updates "around 10:00 a.m. ET", and assuming the later time is the conservative choice (the nowcast never gets credit for an update before it existed).
- **Matched-horizon snapshot: as-of = release − h days (h = 30, 14, 7, 3, 1), capped at Kalshi's last trading time.** Both sources use their latest value visible at as-of: Kalshi's last finished hourly candle, and the last published nowcast (carry-forward).
- **Outcome = first-release value strictly greater than the strike.** That's the contract rule. It agrees with Kalshi's own settlement on all 489 head-to-head strikes.
- **Head-to-head inclusion is computed in SQL (`v_cpi_events.in_headtohead`): ≥5 strikes AND an official MoM print.** One rule in one place, with the reason for each exclusion recorded. Result: 53 releases (Mar 2022 – Sep 2026).
- **The nowcast table holds only nowcasts. The Fed's "actual" values aren't loaded.** Truth comes from one source only (ALFRED first release, which matches Kalshi's settlements).
- **Calibration prices: the view exposes bid, ask, spread, mid and last trade, but applies no quote-quality filter yet.** 28% of calibration rows have a $0 bid (longshots) and 2–5% are empty books (bid 0, ask $1), so the mid is unreliable there. The filtering rule is a Phase 3 decision.
- **SQL logic is tested against a throwaway Postgres schema built from the real `schema.sql`/`views.sql`.** It tests the actual SQL, not a Python re-implementation of it.
- **No browser-UA spoofing or bot-check workarounds.** Sources that block automated access (the Kalshi fee PDF, BLS pages) get verified by hand instead.
- **Treat Kalshi strikes as a survival curve P(CPI > x), not brackets.** The contracts are cumulative "strictly greater than" thresholds. Normalization means monotonicity repair, not overround scaling.
- **No nowcast fallback model needed.** The Cleveland Fed's own daily nowcast archive is publicly available.

## Phase 3
- **Quote filter: drop empty books (bid $0 and ask $1). Primary price = bid/ask midpoint when spread ≤ 10¢.** A midpoint of a wide or empty book isn't a probability anyone would trade at. Robustness checks: all non-empty quotes (any spread), and last trade price. Primary sample: 5,673 prices, 3,340 markets, 1,123 events.
- **Time-to-close horizons: 1, 7 and 30 days, pooled for headline numbers and also reported separately.** Pooling repeats some markets, so all inference is clustered by event.
- **All CIs are event-clustered bootstraps (500 draws, fixed seeds). Regression SEs are cluster-robust by event.** Markets in one event are mechanically linked (one bracket wins), so treating them as independent would overstate precision.
- **Prices are clipped to [0.005, 0.995] before logit transforms.** It avoids infinite log-odds; that's the smallest half-tick a midpoint can take.
- **Categories with < 30 events are pooled into "Other".** Per-category estimates on a handful of events are noise.
- **Calibration regression: logit P(yes) = a + b·logit(price).** Perfect calibration is a = 0, b = 1. b > 1 means prices are pulled toward 50%, i.e. longshots over-priced and favourites under-priced.
- **Mispricing model: logistic regression on logit(price), its interaction with standardized log volume, spread, horizon and category dummies (Sports = reference).** The interaction term directly tests whether calibration slope changes with liquidity.
- **Probability weighting model: price = w(true probability). Fit by maximum likelihood of outcomes under p = w⁻¹(price).** Forms: Tversky–Kahneman (1992) one-parameter and Prelec (1998) one-parameter. That matches the spec's "map true frequencies to prices". γ < 1 is the inverse-S (longshot) distortion.
- **The likelihood is computed on (unique price, #yes, #total) counts.** It's mathematically identical and 14× faster, since midpoints sit on a half-cent grid.
- **Group differences are tested with a Wald test using bootstrap SEs (groups are disjoint event sets, so independent), plus a bootstrap CI for high-minus-low liquidity.** A plain likelihood-ratio test would assume independent observations.
- **Literature comparison values:**
  - TK 1992: γ = 0.61 (gains), 0.69 (losses). Verified from reproductions of the paper's estimates.
  - Camerer & Ho 1994: 0.56. Wu & Gonzalez 1996: 0.71. Both as reported in Gonzalez & Wu (1999).
  - Prelec 1998: α ≈ 0.65 "rough estimate", as reported in Wilcox (2019).
  - The last three are secondary-source citations, and the write-up says so.
- **Charts use the validated reference palette slots 1–3 (blue/orange/aqua) plus a gray reference line. They're responsive.** Light/dark theming waits for the Phase 7 dashboard.
- **Fixed a Phase 2 bug: pre-Dec-2022 CPI markets had no strike in the API (121 markets, 18 events).** The strike is now parsed from the ticker (`TN0.4` = −0.4) and must match the title, or the load fails. The Phase 2 outcome check had silently skipped those NULL rows; it now uses `IS DISTINCT FROM` and also counts NULL strikes. All 489 head-to-head strikes now verified against Kalshi's settlement.

## Phase 4
- **Nowcast → strike probability: P(YES at k) = P(nowcast + error ≥ k + 0.05).** Kalshi pays on the printed one-decimal value, and printed > k ⇔ unrounded ≥ k + 0.05 for strikes on the 0.1 grid.
- **Error distribution estimated with an expanding window at the same horizon, using only months released strictly before the forecast's as-of time (minimum 24 past months).** That's the spec's no-lookahead rule. The history starts Aug 2013, so even the first head-to-head release has ~100 past errors.
- **Error model = Student-t with zero location (`t0`), chosen on pre-sample data only (months released before Mar 2022).** Mean out-of-sample log predictive density: t 0.49 vs normal 0.23 vs normal-with-bias 0.25. t wins at every horizon, but the month-level edge is not decisive (diff 0.27, SE 0.20). Fitted df ≈ 2.3 (very fat tails, driven by 2020). Choosing the model before seeing the test period avoids tuning to the result. A normal-error sensitivity check gives the same conclusions.
- **When the as-of time is capped at Kalshi's last trade (Sep 2025), the error spread is taken from the horizon matching the actual distance to release.** That forecast really was made 9 days out.
- **Kalshi normalization = isotonic (pool-adjacent-violators) regression to a non-increasing survival curve over strikes.** Each strike is a separate binary market, so there's no overround to divide out. The only coherence condition is P(CPI > k) non-increasing in k, and isotonic regression is the least-squares projection onto that set.
- **Both sources are scored on exactly the same strikes: those listed with a bid/ask at the as-of time.**
- **Unit of analysis = one release × horizon. Each source's score is averaged over that release's strikes.** That gives 53 observations per horizon (52 at 30 days), not ~490 strike rows, so brackets are never treated as independent.
- **Scores: mean Brier per strike (primary), RPS, log score.** For cumulative threshold contracts, RPS over the listed thresholds = the sum of per-strike Brier scores. It differs from mean Brier only by weighting releases with more strikes more heavily, so it's reported as a robustness check, not new evidence.
- **Diebold–Mariano test on per-release loss differences in time order.** Newey–West variance (lag = ⌊T^⅓⌋ = 3), Harvey–Leybourne–Newbold small-sample correction, t(T−1) reference. Also reported: a bootstrap CI of the mean difference and the share of releases each source wins.
- **Encompassing: logistic regression of outcomes on logit(p_Kalshi) and logit(p_Fed), SEs clustered by release.** A significant coefficient on one source means it carries information the other lacks.
- **Combination = simple 50/50 average of probabilities.** It's the spec's benchmark, and it needs no estimated weights, so no in-sample fitting advantage.
- **Multiple comparisons: 5 horizons × 3 metrics are reported, and they're highly correlated.** Single p-values near 0.05 are presented as borderline, not as discoveries.

## Phase 5
- **Daily calendar = the days the Fed published a nowcast for that release. Consecutive means ≤ 4 calendar days apart (weekend/holiday).** Changes are computed within a release only, never across releases.
- **Primary alignment: Kalshi snapshot at 09:30 ET, using the last hourly candle finished by then (in practice the candle ending 09:00).** That's strictly before the ~10:00 ET nowcast, so Kalshi's daily change can't contain that day's nowcast. It does contain the previous day's, which is exactly what the "Fed → Kalshi" test needs.
- **Robustness alignment: a 16:00 ET snapshot, after the nowcast.** Same-day co-movement then includes Kalshi reacting to that morning's update and data releases.
- **Kalshi's daily level = the median of its implied distribution of unrounded CPI (isotonic-repaired survival curve, linear interpolation between thresholds).** It's on the same scale as the nowcast. NaN when 50% falls outside the listed strikes (3.4% of days). The median is used instead of the mean because the mean depends on how the tails are truncated and jumps when strikes are added. The mean (only between days with identical strike sets) is a robustness check.
- **Tests: Granger-style panel OLS on daily changes with 2 lags of both series, SEs clustered by release, and joint Wald tests on the cross lags.** Plus cross-correlations at lags −3…+3 with release-level bootstrap CIs.
- **Robustness: drop CPI/PCE data-release days (from the Fed chart's release markers, now the `data_release_markers` table).** On those days both series react to the same public print, which isn't a lead by either.
- **7 lags × 4 variants of cross-correlations are reported. Isolated significant lags that don't survive the robustness checks aren't interpreted as findings.**

## Phase 6
- **Signal = Fed-implied probability (Phase 4 Student-t model, no lookahead) vs Kalshi's actual quote for each strike, at the 5 Phase-4 decision times.** It reuses the probabilities already validated in the head-to-head, so the backtest can't quietly use a different, better-tuned model.
- **Buy YES at the ask if p_fed − ask > θ. Buy NO at (1 − bid) if bid − p_fed > θ. Hold to settlement.** Crossing the spread (taker) is the realistic way to act on a signal immediately. Holding to settlement avoids needing exit prices.
- **Fees: Kalshi taker fee ceil(0.07 × C × P × (1−P)) on 100-contract orders.** That's the published formula, and KXCPI's API shows fee type `quadratic_with_maker_fees`, multiplier 1. 100 contracts makes the round-up-to-the-cent negligible.
- **Today's fee schedule is applied to all years.** Kalshi's historical fee schedules aren't available through the API (`fee_changes` is empty), so earlier fees may have differed. Listed as a limitation.
- **Fills assume the full 100 contracts execute at the quoted best bid/ask.** Hourly candles give no order-book depth, so this is optimistic for thin books. Also a limitation.
- **Train = releases for Mar 2022 – Dec 2024 (34). Test = Jan 2025 – Sep 2026 (19, excluding the two shutdown months).** A time split, never shuffled, so the test is truly out-of-sample.
- **θ chosen on train only: highest total net P&L over θ ∈ {0.00, 0.01, …, 0.30} with ≥ 20 trades.** Total P&L (not mean) avoids picking a θ that trades twice and gets lucky. θ is then applied once to the test period.
- **The chosen θ = 0.15 had negative training P&L (−0.5¢ per contract). It's used anyway and reported as such.** The rule must pick something, and the honest summary is that no training threshold was profitable after costs.
- **P&L inference: release-level bootstrap of mean P&L per contract, and a t-stat on per-release P&L totals.** Strikes in one release settle on the same CPI print, so they aren't independent bets.
- **Cost decomposition reported: (1) midpoint fills, no fees; (2) bid/ask, no fees; (3) bid/ask + fees.** It shows where an edge, if any, is lost.
- **The per-horizon results (longer horizons looking better) are labelled exploratory.** That split wasn't chosen before seeing the data. Confirming it would need new releases.

## Phase 7
- **The dashboard is one static page (`docs/index.html`) generated by `src/viz/build_dashboard.py` from the phase outputs and the database. All narrative numbers are inserted by code.** That meets the spec's "every number produced by code", and editing a result can't leave stale text behind.
- **Charts are drawn client-side by Plotly.js, bundled locally (`docs/assets/plotly.min.js`), with no CDN.** The page works offline and has no third-party dependency. Client-side rendering lets charts re-theme for light/dark mode, which pre-rendered Plotly HTML can't.
- **Palette: validated reference slots 1–3 in separate light and dark steps (validator passes both modes, all pairs).** Light aqua is below 3:1 contrast, so every chart has a legend and every chart's data is downloadable as CSV (the table view).
- **Legends are pinned to the bottom of each chart container (`yref: container`).** Paper-relative legends collided with x-axis titles at some widths.
- **Headline wording follows the statistics: "borderline", not "significantly better".** At 1 day the DM p-value is 0.07 (Brier), and 15 correlated tests are shown.
- **`src/run_all.py` rebuilds database → analyses → dashboard in one command (about 4 minutes).** It's the reproducibility path. Downloads stay separate because they hit external APIs.

# Phase 0: Feasibility Check

Checked 2026-10-27. Every source below was probed live. Raw responses are cached in `data/raw/phase0/`.

## Verdict

**Feasible at $0.** All three data sources work without payment. The Cleveland Fed's daily nowcast history is available directly, so **no fallback model is needed**.

| Source | Works? | Auth | Cost |
|---|---|---|---|
| Kalshi market data (markets, historical, candlesticks) | Yes | None needed | $0 |
| FRED / ALFRED CPI (incl. first-release vintages) | Yes | CSV: none. API: free key | $0 |
| Cleveland Fed daily nowcast history | Yes | None | $0 |
| Kalshi fee schedule | Partly (see blockers) | n/a | $0 |

**Usable head-to-head sample: about 53–55 CPI releases** (Mar 2022 to Sep 2026). That's small. Every result will need wide confidence intervals and clustering by release.

---

## 1. Kalshi API

Base URL: `https://api.elections.kalshi.com/trade-api/v2`

### Authentication
All market-data endpoints used here work **without authentication**: `/series`, `/markets`, `/historical/cutoff`, `/historical/markets`, `/historical/markets/{ticker}/candlesticks`, `/series/{series}/markets/{ticker}/candlesticks`, `/series/fee_changes`.

### Live vs. historical split
`GET /historical/cutoff` returned:
```
market_settled_ts:  2026-08-28T00:00:00Z
trades_created_ts:  2026-08-28T00:00:00Z
orders_updated_ts:  2026-10-13T00:00:00Z
```
- Markets settled **before** `market_settled_ts` are only in `/historical/markets`. Their candles are only at `/historical/markets/{ticker}/candlesticks`.
- Markets settled after it (plus open markets) are in `/markets`, with candles at `/series/{series}/markets/{ticker}/candlesticks`.
- Verified: requesting a historical ticker's candles from the live endpoint returns **HTTP 404**. The cutoff moves forward over time, so ingestion must call `/historical/cutoff` each run and route each ticker accordingly.
- **The two tiers use different field names.** Historical candles use `price.close`, `volume`, `open_interest`. Live candles use `price.close_dollars`, `volume_fp`, `open_interest_fp`. The loader must normalize both.

### CPI series tickers (found via `/series?category=Economics`, 871 series)
Headline CPI is series **`KXCPI`** ("CPI", monthly, source BLS).
- Older events keep the legacy prefix (`CPI-21JUN` … `CPI-24OCT`) but are filed **under series `KXCPI`**. Querying series `CPI` returns 0 markets. From `KXCPI-24NOV` on, events use the `KXCPI-` prefix.
- Market ticker format: `KXCPI-26JUL-T0.2` = "Jul 2026 CPI MoM > 0.2%".

Related series for possible extensions (Cleveland Fed also nowcasts these):

| Series | What | Settled events | First event |
|---|---|---|---|
| `KXCPI` | Headline CPI MoM | 64 | Jun 2021 |
| `KXCPICORE` | Core CPI MoM | ~51 | Jul 2022 |
| `KXCPIYOY` | Headline CPI YoY | ~45 | Dec 2022 |

### KXCPI sample size
- **67 events total**: 64 settled (Jun 2021 → Sep 2026, every month, no gaps) plus 3 open (Oct, Nov, Dec 2026).
  - 62 settled events are in the historical tier and 2 (Aug and Sep 2026) are in the live tier.
- **Strikes per event:** median 8 in the historical tier (range 1–15).
  - Jun 2021 to Feb 2022 had only 1–4 strikes, too few to form a distribution.
  - **55 settled events have ≥5 strikes (Mar 2022 onward).**
- **Liquidity varies a lot over time:**
  - 2022: about 150k–480k contracts per event.
  - 2023–24: about 12k–240k, thin.
  - 2025–26: about 450k–2.3M.
- Kalshi markets usually open well before the nowcast starts: median 62 days before release, minimum 25.

### Contract structure (important for methodology)
KXCPI contracts are **not mutually exclusive brackets**. Each is a cumulative threshold: "CPI MoM **strictly greater than** X%" (`strike_type: greater`). So the prices of one event's strikes form a **survival curve** P(CPI > x), not a set of probabilities summing to 1.
- Bracket probabilities come from differencing adjacent strikes.
- The spec's "overround normalization" becomes **monotonicity repair**: the survival curve must be non-increasing in x.
- The tails beyond the lowest and highest strikes need an explicit rule.

### Candlesticks
- `period_interval` accepts **1 (minute), 60 (hour), 1440 (day)** in both tiers.
- **Max 5,000 candles per request.** A full event at 1-minute resolution was rejected (83,520 candles). 1-minute works if requests are chunked.
- Each candle includes **yes_bid and yes_ask OHLC**, last-trade price OHLC/mean, volume, and open interest. That is enough for a bid/ask-based backtest.
- Candles are **sparse for quiet periods**. Example: CPI-23MAR-T0.4 had 93 hourly candles over about 53 days. Gaps must be forward-filled carefully: carry the last quote, never interpolate.
- **Recommendation:** use hourly candles for CPI markets (about 1,300 per market-event, 1 request each). That's fine enough to align with the nowcast's ~10:00 ET update.

### Rate limits
- Kalshi documents token budgets for authenticated traffic. The Basic tier gets 200 read tokens/s at about 10 tokens/request, so **about 20 requests/s**.
- The docs **don't state a limit for unauthenticated requests**. My probes ran at about 5–7 requests/s with no HTTP 429.
- Plan: about 5 requests/s with exponential backoff on 429.
- Scale: about 600 CPI markets × 1 hourly request each, so a few minutes total.

## 2. CPI truth (settlement source)

### What Kalshi settles on (from the KXCPI contract terms PDF, `assets.kalshi.com/contract_terms/CPI.pdf`)
> The Underlying is "the signed one-month, one-decimal percent change in the seasonally adjusted Consumer Price Index for All Urban Consumers (CPI-U)", and revisions after expiration are not counted.

- **Measure:** CPI-U, all items, **seasonally adjusted**, month-over-month % change.
- **Rounding:** one decimal, **as printed by BLS**.
- **Vintage:** **first release.** Revisions are ignored.
- **Payout:** Yes if the value is **strictly greater** than the strike.
- **If no data is available:** the terms give a fallback formula, a 12-month geometric average of the index. This matters for Oct 2025 (see blockers).

### FRED / ALFRED
- FRED **API** requires a free key (confirmed: request without one returns "api_key is not set").
- The **CSV endpoints need no key**:
  - Current series: `fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL`, 958 rows, 1947 → Sep 2026.
  - Point-in-time vintage: `alfred.stlouisfed.org/graph/alfredgraph.csv?id=CPIAUCSL&vintage_date=YYYY-MM-DD`.
- **Spot check: first-release MoM from ALFRED matches Kalshi's settled value in 3/3 cases:**

| Month | ALFRED first release (unrounded) | Rounded | Kalshi settlement |
|---|---|---|---|
| Jun 2022 | 1.322% | 1.3 | 1.3 |
| Jun 2025 | 0.287% | 0.3 | 0.3 (Yes on >0.2, No on >0.3) |
| Jul 2026 | 0.074% | 0.1 | 0.1 |

- **Recommendation:** use Kalshi's own `expiration_value` as the primary truth (it's literally what settled). Cross-check it against ALFRED first-release for every event in Phase 1. Any mismatch gets flagged, not silently fixed.

## 3. Cleveland Fed nowcast history: AVAILABLE

- The nowcasting page's chart loads **`https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json`**. It's a public 7.7 MB file with no auth. Sibling files exist for quarterly and annual nowcasts.
- Structure: a list of **160 target months (Jul 2013 → Oct 2026)**. Each has dated daily series for:
  - Nowcasts: CPI, Core CPI, PCE, Core PCE (MoM %, SA, unrounded).
  - "Actual" values for the same four measures.
- **This is the daily sequence for each past month, not just final values.** Median 28 business-day nowcasts per target month (range 26–53).
- **Horizon coverage vs. the release date** (55 events with ≥5 strikes):
  - First nowcast: median **42** days before release (range 37–45).
  - Last nowcast: median **5** days before release (range 2–6).
  - **So the spec's 1-day and 3-day horizons aren't directly in the file.** Options: (a) carry the last available nowcast forward, which is legitimate because it's what was knowable then; or (b) drop those horizons. Needs your call. I'll also verify the cut-off by snapshotting the file daily around the Nov 10, 2026 release.
- **Real-time vs. revised:**
  - The Cleveland Fed page and User's Guide don't say whether chart history is real-time or re-estimated.
  - Test: compared today's file against the Internet Archive's copy from **2026-03-25**. **All values for all 153 overlapping months are byte-identical**, including the partial Mar 2026 month.
  - So history isn't being rewritten. That's strong evidence of a real-time archive, but it doesn't prove the 2021–22 values were never re-estimated before Mar 2026.
  - An older snapshot (2022–23) exists. The archive rate-limited my requests (HTTP 429), so that check is pending for Phase 1.
- Quirks: date labels are `MM/DD` with no year (they wrap into the next year for Nov/Dec targets). Publish time is about 10:00 a.m. ET each business day, per the page.

## 4. Kalshi fees

- From the API: series `KXCPI` has `fee_type: quadratic_with_maker_fees`, `fee_multiplier: 1`. `GET /series/fee_changes?series_ticker=KXCPI` returned an empty list (no scheduled changes).
- Published formula as reported by multiple secondary sources:
  - **Taker:** fee = ceil-to-cent( 0.07 × C × P × (1 − P) ).
  - **Maker:** 0.0175 × C × P × (1 − P), only on series with maker fees (KXCPI is one), charged only when the resting order fills.
  - No settlement fee.
  - Example: 100 contracts at $0.50 → $1.75 taker fee.
- **Not yet verified against the primary source.** `kalshi.com/docs/kalshi-fee-schedule.pdf` returned HTTP 429 (bot protection) to both automated fetches. Sources disagree on its effective date (Feb 5 2026 vs Jul 7 2026).
- Limitation: the backtest will apply today's fee schedule to 2022–2024 trades. Kalshi's fees in those years may have differed. This goes in the limitations section.

## Blockers and open items

1. **Python isn't installed** (only the Windows Store stub). **PostgreSQL isn't installed.** Both are free; you'll need to install them before Phase 1. Phase 0 probes used curl and Node.js, which are already on your machine. Git is present.
2. **Fee PDF:** please open `https://kalshi.com/docs/kalshi-fee-schedule.pdf` in your browser and confirm the formula and effective date (it blocks automated downloads).
3. **Oct 2025 CPI was never published** (government shutdown; FRED shows a blank). Kalshi still settled `KXCPI-25OCT` at 0.2 on 2025-11-22, apparently via its fallback rule. **Recommend excluding it** from the head-to-head: there's no real CPI print to forecast. Nov 2025 (released late on 2025-12-18, with no October base) needs a check too.
4. **1-day / 3-day horizons** aren't covered by the nowcast file (see §3). Needs a decision.
5. **Thin 2023–24 liquidity** means stale quotes and wide spreads in those years. This affects calibration and backtest realism. It isn't a blocker, but expect noisier results.

## Nothing found that costs money.

## Reproduce
- `node phase0/probe_kalshi.mjs KXCPI KXCPIYOY KXCPICORE` re-runs the Kalshi counts from the cache in `data/raw/phase0/kalshi/`.
- The other checks were one-off curl calls. Their raw outputs are in `data/raw/phase0/{fred,cleveland}/`.

# Project Spec: Crowd vs. the Fed: Who Predicts Inflation Better?

**Framing:** Kalshi prediction markets vs. a Federal Reserve inflation nowcasting model (the Cleveland Fed's, named in the methodology section; the public-facing title is "Crowd vs. the Fed").

**Owner:** Luke (this spec covers Luke's portion only. A teammate separately owns the Kalshi vs. Polymarket cross-venue comparison. Keep a `venue` column in the schema so that work can plug in later.)

---

## 1. Goal

Build a reproducible, research-grade data science project that:

1. Measures how well-calibrated Kalshi prediction markets are overall (calibration foundation).
2. **Headline:** compares Kalshi's monthly CPI markets against the Cleveland Fed's daily inflation nowcast. Which is more accurate, at which horizons, and does either contain information the other lacks?
3. Measures which one moves first when new information arrives (lead-lag analysis).
4. Tests whether any mispricing survives realistic trading costs (backtest).
5. Ships as a public GitHub repo plus a free, static, interactive dashboard on GitHub Pages.

The output should be honest. Null results ("the market and the Fed model are equally accurate") are valid findings and must be reported as such. Never fabricate, smooth over, or cherry-pick results.

---

## 2. Hard constraints

- **Cost: $0.** Free data sources only. No paid APIs, no paid scrapers (e.g., Apify), no paid hosting. If something requires payment, stop and tell Luke.
- **Scripts run locally.** Write Python scripts that Luke runs on his machine for data pulls. Do not read large datasets into the conversation. Inspect data with summaries (`.head()`, `.describe()`, row counts, SQL aggregates).
- **Cache everything.** Save every raw API response to `data/raw/` (JSON) so nothing is downloaded twice. All ingestion scripts must be resumable and idempotent.
- **Secrets.** Any API keys go in a `.env` file that is listed in `.gitignore`. Never commit keys.
- **No lookahead bias.** Any parameter used to make a probability on date *t* (e.g., nowcast error variance) must be estimated only from data available before *t*.
- **Work in phases.** Complete one phase at a time. At the end of each phase, stop and give Luke a short summary: what was built, key numbers, problems, and what's next. Wait for his go-ahead.
- **Log decisions.** Record every non-obvious methodological choice in `DECISIONS.md` (one line each: decision + reason). Luke needs to be able to explain these in interviews.

---

## 3. Stack

- Python 3.11+
- `requests`, `pandas`, `numpy`, `python-dotenv`
- PostgreSQL (local) via `psycopg` / `SQLAlchemy`
- `statsmodels`, `scipy`, `scikit-learn` (calibration utilities only)
- `plotly` for all charts
- `pytest` for tests
- Dashboard: static HTML (Plotly charts exported to HTML) deployed on GitHub Pages. No server.

## 4. Repo structure

```
kalshi-vs-fed/
├── README.md              # findings first, then method, limitations, how to reproduce
├── DECISIONS.md
├── PROJECT_SPEC.md
├── .env.example
├── requirements.txt
├── sql/
│   ├── schema.sql
│   └── views.sql
├── src/
│   ├── ingest/            # kalshi.py, fred.py, cleveland_nowcast.py
│   ├── load/              # load raw JSON/CSV into Postgres
│   ├── analysis/          # calibration.py, cpi_headtohead.py, leadlag.py, backtest.py
│   └── viz/               # build_dashboard.py
├── tests/
├── data/raw/              # gitignored
├── data/processed/        # gitignored except small summary CSVs used by the site
└── docs/                  # GitHub Pages site output
```

---

## 5. Phases

### Phase 0: Feasibility check (do this first, build nothing else)

Verify every data source works before writing the pipeline. Report findings to Luke and stop.

1. **Kalshi API**
   - Confirm which market-data endpoints work without authentication.
   - Call `GET /historical/cutoff` and document how the live vs. historical split works. Markets settled before the cutoff are only available through `/historical/markets` and `/historical/markets/{ticker}/candlesticks`.
   - Find the series/event tickers for monthly CPI markets (search the series/events endpoints in the Economics category; do not guess tickers). Report: date range covered, number of CPI releases with markets, typical number of bracket markets per release, and candlestick granularity available.
   - Document rate limits.
2. **FRED API**
   - Luke will get a free API key. Confirm access to CPI (headline, seasonally adjusted) and, if needed, ALFRED real-time vintages so we use the **first-release** CPI value Kalshi markets settled on.
   - Confirm exactly which CPI measure and rounding Kalshi's CPI contracts settle on (read the contract rules). Our "truth" must match the settlement source exactly.
3. **Cleveland Fed nowcast history**
   - The inflation nowcasting page (clevelandfed.org/indicators-and-data/inflation-nowcasting) has a charting tool that plots past nowcast sequences for previous months. Find the underlying data source the chart uses (e.g., a JSON/CSV endpoint) and confirm we can get the **daily sequence of nowcasts for each past month**, not just final values.
   - **Fallback if unavailable:** report back before doing anything. The fallback is reimplementing the documented model (Knotek & Zaman working paper; inputs are monthly CPI/PCE components plus daily Brent oil and weekly retail gasoline from free FRED/EIA data). Do not start the fallback without Luke's approval.
4. **Kalshi fees:** look up the current published fee schedule for the backtest. Do not assume.

**Phase 0 deliverable:** a short `FEASIBILITY.md` with what works, sample sizes, and any blockers.

### Phase 1: Ingestion

- `src/ingest/kalshi.py`: pull all settled markets (for calibration) plus candlesticks. For CPI markets pull the finest free granularity available (hourly if possible, else daily). Handle the live/historical split, pagination, rate limits (backoff + retry), and resume from cache.
- For the broad calibration set, a daily candlestick is sufficient. If the full market universe is very large, sample sensibly (e.g., markets with nonzero volume) and log the rule in `DECISIONS.md`.
- `src/ingest/fred.py`: CPI first-release values (and any FRED/EIA series needed).
- `src/ingest/cleveland_nowcast.py`: daily nowcast history per target month.

### Phase 2: Database

`sql/schema.sql`, at minimum:

- `markets` (venue, ticker, event_ticker, series_ticker, category, title, open_time, close_time, settlement_time, result, bracket_low, bracket_high, volume, open_interest)
- `candles` (venue, ticker, ts, yes_bid, yes_ask, mid, last_price, volume, open_interest)
- `cpi_releases` (reference_month, release_ts, first_release_value, measure)
- `nowcasts` (reference_month, nowcast_date, measure, value)

`sql/views.sql`: analysis-ready views written in SQL (joins, horizon calculation = days before release, bracket probability panels). The SQL should be clean and readable; it's a resume skill.

### Phase 3: Analysis A: Calibration foundation

- Brier score, log loss, reliability diagrams for all settled markets, by category and by time-to-close.
- Favorite-longshot bias test: regress outcome on logit(price) (calibration slope/intercept); also binned observed vs. implied frequency with confidence intervals.
- Mispricing model: logistic regression of outcome on logit(price) plus liquidity (volume, spread), time-to-close, category. Cluster standard errors by event.
- **Prospect theory probability weighting:** fit a probability weighting function (Tversky-Kahneman 1992 one-parameter form and Prelec 1998) mapping true outcome frequencies to Kalshi prices, by maximum likelihood with bootstrapped confidence intervals. Report the estimated distortion parameter, compare it to the classic values in the literature (look these up and cite them; do not guess), and test whether distortion differs by category and liquidity. Plot the fitted weighting curve against the 45-degree line.

### Phase 4: Analysis B (headline): Kalshi vs. Cleveland Fed on CPI

1. **Convert the nowcast to bracket probabilities.** The nowcast is a point estimate; Kalshi trades brackets. Model actual minus nowcast as a distribution whose spread depends on horizon (days before release). Estimate it with an expanding window using only past errors (no lookahead). Start with a normal; check whether fat tails (Student-t) fit better and record the choice.
2. Also normalize Kalshi bracket prices to a proper probability distribution per release (handle the overround) and document the method.
3. **Head-to-head accuracy** at matched horizons (e.g., 30, 14, 7, 3, 1 days before release): Brier score and ranked probability score (RPS) for each source. Test differences with a Diebold-Mariano test, clustering/HAC adjusted.
4. **Forecast encompassing:** regress outcomes on both forecasts. Does Kalshi add information beyond the nowcast, and vice versa? Also test a simple 50/50 combination.
5. Be explicit about sample size (number of CPI releases). Cluster by release; do not treat brackets as independent observations.

### Phase 5: Analysis C: Lead-lag

- Build a daily series of Kalshi's implied expected CPI value (from bracket probabilities) and the nowcast.
- Test which moves first: lagged regressions / Granger-style tests on daily changes. Account for timing: the nowcast updates around 10:00 a.m. ET each business day, so align Kalshi snapshots carefully and document the alignment.

### Phase 6: Analysis D: Backtest

- Simple rule: when the nowcast-implied probability for a bracket differs from Kalshi's price by more than a threshold, take the position. Use the actual bid/ask (not mid) and the real fee schedule.
- Report P&L, hit rate, number of trades, and sensitivity to the threshold. Use a held-out period for the final threshold. If the edge disappears after costs, say so clearly.

### Phase 7: Dashboard and write-up

- Static site in `docs/`, deployable to GitHub Pages, self-contained, no server.
- Sections: headline finding; calibration curves; Kalshi vs. Fed accuracy by horizon; an interactive "pick a CPI release" chart showing both probability paths converging to the outcome; lead-lag; backtest; methodology and limitations.
- `README.md`: lead with 3 to 5 concrete findings with numbers, then data sources, method, limitations, and reproduction steps.

---

## 6. Quality bar

- Tests for: the nowcast-to-bracket probability conversion, overround normalization, scoring functions, and horizon alignment.
- Every chart and number on the site must be produced by code in the repo from the database.
- Limitations section must cover: small number of CPI releases, Kalshi liquidity, settlement-definition matching, and any fallback used for the nowcast.

# Crowd vs. the Fed: Who Predicts Inflation Better?

Kalshi lets anyone trade on next month's CPI print. The Cleveland Fed publishes a daily model-based nowcast of the same number. This project compares them like-for-like across **53 monthly CPI releases (Mar 2022 – Sep 2026)**: same strikes, same moment, no hindsight. It also asks how well-calibrated Kalshi is overall and whether the disagreement can be traded after costs.

**Dashboard:** `docs/index.html` (static; deploy with GitHub Pages, see below).

## Findings

1. **Near release, the crowd is more accurate, but the evidence is borderline.**
   - One day before release, Kalshi's mean Brier score is **0.068 vs 0.091** for the Fed nowcast, about **25% lower**.
   - Kalshi wins 58% of releases.
   - Diebold–Mariano test: **p = 0.07** (Brier), 0.04 (ranked probability score), 0.05 (log score).
   - The advantage holds at 3, 7 and 14 days but isn't significant there.
2. **A month out, the Fed holds its own, and the average of the two is best.**
   - At 30 days the Fed is slightly ahead (0.117 vs 0.131, p = 0.43).
   - A 50/50 average beats Kalshi alone (p = 0.036).
3. **The Fed adds information only far from the release.**
   - In an encompassing regression, the nowcast adds nothing beyond Kalshi's price 1 day out (p = 0.62).
   - At 30 days it carries independent information (coefficient 0.44, p = 0.006).
4. **Day to day, neither moves first (null result).**
   - Kalshi following the Fed's last update: p = 0.24. The Fed following Kalshi: p = 0.98.
   - The nowcast is unchanged on 72% of days.
   - The little co-movement there is comes from both reacting to the same CPI/PCE data releases.
5. **Kalshi is well calibrated, with a mild favorite-longshot bias.** This is measured across 5,673 prices from 1,123 events exchange-wide:
   - Contracts priced ≤5¢ averaged 2.0¢ but happened 0.9% of the time.
   - Calibration slope is 1.11 (SE 0.05).
   - The fitted Tversky–Kahneman probability-weighting parameter is **γ = 0.91 [0.84, 0.97]**. That's real distortion, but much milder than the classic laboratory estimate of 0.61.
6. **The disagreement can't be traded profitably after costs.**
   - Rule: buy when the Fed's probability is more than θ away from Kalshi's bid/ask, hold to settlement, pay Kalshi's taker fee.
   - No threshold was profitable in training (34 releases).
   - The chosen θ = 0.15 made **+4.9¢ per contract on 19 held-out releases (95% CI −10.6¢ to +23.1¢)**, which is indistinguishable from zero.
   - At midpoint prices the same signal earned +8.7¢ in training. The bid-ask spread is what eats it.

Every number above is produced by the code in this repo from the project database (`outputs/phase3` … `outputs/phase6`).

## Data (all free)

| Source | What | Access |
|---|---|---|
| [Kalshi API](https://docs.kalshi.com) | Every settled `KXCPI` market (headline CPI MoM) with hourly bid/ask candles. A seeded random sample of 6,020 markets across all categories for calibration. | Public endpoints, no key. Live and historical tiers are handled separately. |
| [Cleveland Fed inflation nowcasting](https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting) | Daily nowcast sequence for every target month since Jul 2013 (the JSON file behind the site's chart) | Public. Verified unchanged against an Internet Archive copy from 7 months earlier. |
| [ALFRED](https://alfred.stlouisfed.org) (St. Louis Fed) | First-release CPI-U (SA), with exact release dates | Vintage CSVs, no key needed |

**Truth matches the settlement rule exactly.** Kalshi pays on the first-published, one-decimal, seasonally adjusted CPI-U monthly change, ignoring revisions. ALFRED's release-day vintage matched Kalshi's settled value in 62 of 62 checkable releases.

## Method

- **Fed probabilities.**
  - Kalshi strike *k* pays if CPI prints above *k*, i.e. if the unrounded change is ≥ *k* + 0.05.
  - P(YES) = P(nowcast + error ≥ *k* + 0.05), with a Student-t error distribution.
  - The error distribution is estimated by expanding window only from months released before the forecast moment, at the same horizon.
  - Student-t was chosen over the normal on pre-sample data (before Mar 2022) only.
- **Kalshi probabilities.** Bid/ask midpoints, repaired with isotonic regression so P(CPI > *k*) never rises with *k*. Separate binary markets have no overround to remove.
- **Matched horizons.** Both sources use their latest value published at or before release − *h* days (*h* = 30, 14, 7, 3, 1), never after Kalshi's last trade.
- **Statistics.**
  - One score per release per source, so strikes aren't treated as independent.
  - Diebold–Mariano tests with Newey–West variance and the Harvey–Leybourne–Newbold correction.
  - Encompassing and calibration regressions with SEs clustered by release/event.
  - Event-level bootstrap intervals.
  - Prospect-theory weighting functions (Tversky–Kahneman 1992; Prelec 1998) fit by maximum likelihood.
- **No lookahead.** Kalshi candles are timestamped at period end, and a value is used only if published at or before the as-of time. Tests check this explicitly.

Every methodological choice and its reason is in [DECISIONS.md](DECISIONS.md). The data feasibility study is in [FEASIBILITY.md](FEASIBILITY.md).

## Limitations

- **Small sample:** 53 releases. Effects of this size are hard to confirm, so p-values near 0.05 are reported as borderline. Many correlated tests are shown; a single significant one shouldn't be over-read.
- **Nowcast archive gap:** the Fed archive has no values for the last 2–3 business days before each release, so the 1–3 day comparisons use a nowcast about 4 days old (carried forward). No fallback model was needed or used. Kalshi's lead also appears at 7–14 days, where the nowcast is fresh.
- **Kalshi liquidity:** some 2023–24 CPI events were thin. Calibration uses midpoints of books with spreads up to 10¢. The backtest assumes full fills at the quoted bid/ask, because there's no order-book depth data.
- **Settlement-definition matching:** Oct and Nov 2025 have no official monthly print (federal shutdown) and are excluded. Sep 2025 was released 9 days late, so both sources are cut off at Kalshi's last trade.
- **Fees:** today's published taker fee (0.07 × C × P × (1−P), rounded up) is applied to all years. Historical schedules aren't available via the API.
- **Error model:** the Fed's probabilities depend on the chosen error distribution. A normal-error version gives the same conclusions.

## Reproduce

Requirements: Python 3.11+, PostgreSQL 16 (local). Everything is free. The raw cache is about 5 GB.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # on macOS/Linux: .venv/bin/python
cp .env.example .env                                         # then set PGPASSWORD; create database kalshi_vs_fed
```

Download data. Scripts are resumable, idempotent, and cache every raw response in `data/raw/`:

```bash
.venv/Scripts/python -m src.ingest.kalshi cpi
.venv/Scripts/python -m src.ingest.kalshi calibration        # ~30 min, rate-limited
.venv/Scripts/python -m src.ingest.fred
.venv/Scripts/python -m src.ingest.cleveland_nowcast
.venv/Scripts/python -m src.ingest.validate                  # cross-source checks
```

Build the database, run every analysis, and build the dashboard (about 4 minutes):

```bash
.venv/Scripts/python -m src.run_all
.venv/Scripts/python -m pytest
```

View the dashboard locally:

```bash
.venv/Scripts/python -m http.server 8765 --directory docs
```

**Deploy:** push to GitHub, then go to *Settings → Pages → Deploy from a branch → `main` / `/docs`*.

## Repo layout

```
sql/            schema.sql (tables), views.sql (analysis views), checks.sql (post-load sanity checks)
src/ingest/     kalshi.py, fred.py, cleveland_nowcast.py, validate.py, http_cache.py
src/load/       load_db.py (raw cache -> Postgres)
src/analysis/   calibration.py, cpi_headtohead.py, leadlag.py, backtest.py + scoring, weighting, fees, nowcast_probs
src/viz/        build_dashboard.py, template.html
outputs/        result tables and charts per phase
docs/           the static dashboard (GitHub Pages)
tests/          pytest suite (scoring, probability conversion, normalization, horizon alignment, backtest mechanics)
```

The schema includes a `venue` column so cross-venue data (e.g. Polymarket) can be added to the same tables.

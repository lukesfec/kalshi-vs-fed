-- Sanity checks run after every load:  psql -f sql/checks.sql
\pset footer off

\echo '--- 1. CPI events: head-to-head sample'
SELECT in_headtohead, exclusion_reason, count(*) AS events,
       min(reference_month) AS first_month, max(reference_month) AS last_month
FROM v_cpi_events
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

\echo '--- 2. Panel coverage by horizon (one row per release x strike)'
SELECT horizon_days,
       count(DISTINCT event_ticker)                                        AS releases,
       count(*)                                                            AS strike_rows,
       round(avg(strike_listed::int), 3)                                   AS share_listed,
       round(avg((mid IS NOT NULL)::int) FILTER (WHERE strike_listed), 3)  AS share_with_bid_ask,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM quote_age) / 3600)
           FILTER (WHERE strike_listed)                                    AS median_quote_age_hours,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM nowcast_age) / 86400) AS median_nowcast_age_days,
       max(extract(epoch FROM nowcast_age) / 86400)                        AS max_nowcast_age_days,
       sum(capped_at_last_trade::int)                                      AS capped_rows
FROM v_cpi_horizon_panel
GROUP BY horizon_days
ORDER BY horizon_days DESC;

\echo '--- 3. Outcome in panel must equal Kalshi settlement for every strike (expect 0 and 0)'
-- IS DISTINCT FROM: a NULL outcome counts as a disagreement instead of being silently skipped.
SELECT count(*) AS rows_checked,
       sum((p.outcome_yes IS DISTINCT FROM (m.result = 'yes'))::int) AS disagreements,
       sum((p.strike IS NULL)::int) AS null_strikes
FROM v_cpi_horizon_panel p
JOIN markets m ON m.ticker = p.ticker
WHERE p.horizon_days = 1;

\echo '--- 3b. Every CPI market has a strike (expect 0)'
SELECT count(*) AS cpi_markets_without_strike FROM markets WHERE in_cpi_set AND bracket_low IS NULL;

\echo '--- 4. No lookahead: nothing used after as-of (expect 0 and 0)'
SELECT sum((quote_ts > asof_ts)::int) AS quotes_after_asof,
       sum((nowcast_age < interval '0')::int) AS nowcasts_after_asof
FROM v_cpi_horizon_panel;

\echo '--- 5. Sep 2025 (late release): capped at Kalshi last trade'
SELECT horizon_days, min(target_ts) AS target_ts, min(asof_ts) AS asof_ts, bool_and(capped_at_last_trade) AS capped
FROM v_cpi_horizon_panel
WHERE event_ticker = 'KXCPI-25SEP'
GROUP BY 1
ORDER BY 1 DESC;

\echo '--- 6. Calibration points by horizon'
SELECT horizon_days, count(*) AS points, count(DISTINCT event_ticker) AS events,
       count(DISTINCT category) AS categories,
       round(avg((mid IS NOT NULL)::int), 3) AS share_with_bid_ask,
       round(avg(spread), 3) AS mean_spread,
       round(avg(outcome_yes::int), 3) AS share_yes
FROM v_calibration_points
GROUP BY 1
ORDER BY 1;

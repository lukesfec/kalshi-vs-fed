-- Crowd vs. the Fed: analysis-ready views.
-- Rule used throughout: a value is visible at time t only if it was published at or before t.
--   Kalshi candle  -> visible once its period has ended   (candles.ts = period END)
--   Nowcast        -> visible from 10:00 ET on its date   (nowcasts.nowcast_ts)
--   CPI release    -> 08:30 ET on release day             (cpi_releases.release_ts)


-- One row per Kalshi CPI event (= one monthly CPI release).
CREATE VIEW v_cpi_events AS
WITH ev AS (
    SELECT
        event_ticker,
        to_date(right(event_ticker, 5), 'YYMON')  AS reference_month,   -- 'KXCPI-26JUL' -> 2026-07-01
        count(*)                                   AS n_strikes,
        min(open_time)                             AS first_open_ts,
        max(close_time)                            AS last_trade_ts,
        min(expiration_value)                      AS kalshi_settled_value,
        sum(volume)                                AS event_volume
    FROM markets
    WHERE in_cpi_set
    GROUP BY event_ticker
)
SELECT
    ev.*,
    r.release_ts,
    r.first_release_value,
    -- Head-to-head sample rules (see DECISIONS.md)
    (r.first_release_value IS NOT NULL AND ev.n_strikes >= 5) AS in_headtohead,
    CASE
        WHEN r.first_release_value IS NULL THEN 'no official MoM print (2025 shutdown)'
        WHEN ev.n_strikes < 5              THEN 'fewer than 5 strikes'
    END AS exclusion_reason
FROM ev
LEFT JOIN cpi_releases r
       ON r.reference_month = ev.reference_month
      AND r.measure = 'CPI-U SA MoM %';


-- Every hourly quote for every CPI strike, with its distance to the release.
CREATE VIEW v_cpi_hourly_quotes AS
SELECT
    e.event_ticker,
    e.reference_month,
    m.ticker,
    m.bracket_low                                           AS strike,   -- contract pays if CPI > strike
    c.ts,
    c.yes_bid,
    c.yes_ask,
    c.mid,
    c.last_price,
    c.volume,
    c.traded,
    e.release_ts,
    extract(epoch FROM (e.release_ts - c.ts)) / 86400.0     AS days_before_release,
    (e.first_release_value > m.bracket_low)                 AS outcome_yes
FROM candles c
JOIN markets m      ON m.venue = c.venue AND m.ticker = c.ticker
JOIN v_cpi_events e ON e.event_ticker = m.event_ticker
WHERE m.in_cpi_set
  AND c.period_minutes = 60;


-- Matched-horizon panel for the head-to-head: one row per (release, horizon, strike).
-- as-of time = release_ts - horizon, but never later than Kalshi's last trading time, so
-- neither source sees information the other couldn't (matters for Sep 2025, released 9
-- days after Kalshi stopped trading). Both sources use the latest value visible at as-of.
CREATE VIEW v_cpi_horizon_panel AS
WITH horizons(horizon_days) AS (
    VALUES (30), (14), (7), (3), (1)
),
snap AS (
    SELECT
        e.event_ticker,
        e.reference_month,
        e.release_ts,
        e.first_release_value,
        h.horizon_days,
        e.release_ts - make_interval(days => h.horizon_days)                         AS target_ts,
        LEAST(e.release_ts - make_interval(days => h.horizon_days), e.last_trade_ts) AS asof_ts
    FROM v_cpi_events e
    CROSS JOIN horizons h
    WHERE e.in_headtohead
)
SELECT
    s.event_ticker,
    s.reference_month,
    s.horizon_days,
    s.target_ts,
    s.asof_ts,
    (s.asof_ts < s.target_ts)                     AS capped_at_last_trade,
    m.ticker,
    m.bracket_low                                 AS strike,
    (m.open_time <= s.asof_ts)                    AS strike_listed,
    k.ts                                          AS quote_ts,
    s.asof_ts - k.ts                              AS quote_age,
    k.yes_bid,
    k.yes_ask,
    k.mid,
    k.last_price,
    nc.nowcast_date,
    s.asof_ts - nc.nowcast_ts                     AS nowcast_age,
    nc.value                                      AS nowcast_value,
    s.first_release_value,
    (s.first_release_value > m.bracket_low)       AS outcome_yes
FROM snap s
JOIN markets m
  ON m.event_ticker = s.event_ticker
 AND m.in_cpi_set
LEFT JOIN LATERAL (                     -- Kalshi: last hourly candle that had ended by as-of
    SELECT c.ts, c.yes_bid, c.yes_ask, c.mid, c.last_price
    FROM candles c
    WHERE c.venue = m.venue
      AND c.ticker = m.ticker
      AND c.period_minutes = 60
      AND c.ts <= s.asof_ts
    ORDER BY c.ts DESC
    LIMIT 1
) k ON true
LEFT JOIN LATERAL (                     -- Fed: last nowcast published by as-of (carry-forward)
    SELECT n.nowcast_date, n.nowcast_ts, n.value
    FROM nowcasts n
    WHERE n.reference_month = s.reference_month
      AND n.measure = 'CPI'
      AND n.nowcast_ts <= s.asof_ts
    ORDER BY n.nowcast_ts DESC
    LIMIT 1
) nc ON true;


-- Calibration set: each market's price at fixed times before close, from daily candles.
-- Markets that didn't exist yet at a horizon simply have no row for it.
CREATE VIEW v_calibration_points AS
WITH horizons(horizon_days) AS (
    VALUES (1), (7), (30)
)
SELECT
    m.ticker,
    m.event_ticker,
    m.series_ticker,
    m.category,
    m.volume                                          AS market_volume,
    h.horizon_days,
    m.close_time - make_interval(days => h.horizon_days) AS asof_ts,
    k.ts                                              AS quote_ts,
    k.yes_bid,
    k.yes_ask,
    k.yes_ask - k.yes_bid                             AS spread,
    k.mid,
    k.last_price,
    COALESCE(k.mid, k.last_price)                     AS price,
    (m.result = 'yes')                                AS outcome_yes
FROM markets m
CROSS JOIN horizons h
JOIN LATERAL (
    SELECT c.ts, c.yes_bid, c.yes_ask, c.mid, c.last_price
    FROM candles c
    WHERE c.venue = m.venue
      AND c.ticker = m.ticker
      AND c.period_minutes = 1440
      AND c.ts <= m.close_time - make_interval(days => h.horizon_days)
    ORDER BY c.ts DESC
    LIMIT 1
) k ON true
WHERE m.in_calibration_set;


-- Cleveland Fed track record: for every month with a first release (2013 onward), the
-- nowcast visible at each horizon before that release, and its error vs. the first print.
-- Feeds the expanding-window error model that turns a point nowcast into probabilities.
CREATE VIEW v_nowcast_errors AS
WITH horizons(horizon_days) AS (
    VALUES (30), (14), (7), (3), (1)
)
SELECT
    r.reference_month,
    r.release_ts,
    h.horizon_days,
    r.release_ts - make_interval(days => h.horizon_days)  AS asof_ts,
    nc.nowcast_date,
    nc.value                                              AS nowcast_value,
    r.first_release_unrounded                             AS actual_unrounded,
    r.first_release_unrounded - nc.value                  AS error
FROM cpi_releases r
CROSS JOIN horizons h
JOIN LATERAL (
    SELECT n.nowcast_date, n.value
    FROM nowcasts n
    WHERE n.reference_month = r.reference_month
      AND n.measure = 'CPI'
      AND n.nowcast_ts <= r.release_ts - make_interval(days => h.horizon_days)
    ORDER BY n.nowcast_ts DESC
    LIMIT 1
) nc ON true
WHERE r.first_release_unrounded IS NOT NULL;


-- Daily Kalshi snapshots on the nowcast calendar, for lead-lag analysis.
-- For every head-to-head release and every day the Fed published a CPI nowcast for it,
-- take each strike's last hourly quote visible at two clock times (ET):
--   pre_nowcast  09:30  - before that day's ~10:00 nowcast exists (primary alignment)
--   after_close  16:00  - after it (robustness)
-- Snapshots after Kalshi's last trade or after the release itself are dropped.
CREATE VIEW v_cpi_daily_snapshots AS
WITH snap_times(snapshot, clock_et) AS (
    VALUES ('pre_nowcast', time '09:30'), ('after_close', time '16:00')
),
days AS (
    SELECT
        e.event_ticker,
        e.reference_month,
        e.release_ts,
        e.last_trade_ts,
        n.nowcast_date,
        n.nowcast_ts,
        n.value AS nowcast_value,
        s.snapshot,
        (n.nowcast_date + s.clock_et) AT TIME ZONE 'America/New_York' AS snapshot_ts
    FROM v_cpi_events e
    JOIN nowcasts n
      ON n.reference_month = e.reference_month
     AND n.measure = 'CPI'
    CROSS JOIN snap_times s
    WHERE e.in_headtohead
)
SELECT
    d.event_ticker,
    d.reference_month,
    d.nowcast_date,
    d.nowcast_value,
    d.snapshot,
    d.snapshot_ts,
    m.ticker,
    m.bracket_low AS strike,
    k.ts          AS quote_ts,
    k.yes_bid,
    k.yes_ask,
    k.mid
FROM days d
JOIN markets m
  ON m.event_ticker = d.event_ticker
 AND m.in_cpi_set
 AND m.open_time <= d.snapshot_ts
JOIN LATERAL (
    SELECT c.ts, c.yes_bid, c.yes_ask, c.mid
    FROM candles c
    WHERE c.venue = m.venue
      AND c.ticker = m.ticker
      AND c.period_minutes = 60
      AND c.ts <= d.snapshot_ts
    ORDER BY c.ts DESC
    LIMIT 1
) k ON true
WHERE d.snapshot_ts <= d.last_trade_ts
  AND d.snapshot_ts <  d.release_ts
  AND k.mid IS NOT NULL;

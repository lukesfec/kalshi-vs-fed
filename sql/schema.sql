-- Crowd vs. the Fed: core tables.
-- Rebuilt from the raw cache by src/load/load_db.py; never edited by hand.
-- All timestamps are timestamptz (stored UTC). Release/publish times are converted from ET in the loader.

DROP VIEW IF EXISTS
    v_cpi_daily_snapshots, v_nowcast_errors, v_calibration_points, v_cpi_horizon_panel, v_cpi_hourly_quotes, v_cpi_events CASCADE;
DROP TABLE IF EXISTS candles, markets, cpi_releases, nowcasts, data_release_markers CASCADE;

-- One row per prediction-market contract.
-- `venue` lets the teammate's Polymarket data plug into the same tables later.
CREATE TABLE markets (
    venue              text        NOT NULL DEFAULT 'kalshi',
    ticker             text        NOT NULL,
    event_ticker       text        NOT NULL,
    series_ticker      text        NOT NULL,
    category           text,
    title              text,
    strike_type        text,          -- 'greater' = pays if value > bracket_low; 'between', 'less', ...
    bracket_low        numeric,       -- Kalshi floor_strike (exclusive for 'greater')
    bracket_high       numeric,       -- Kalshi cap_strike
    open_time          timestamptz,
    close_time         timestamptz,   -- last trading time
    settlement_time    timestamptz,
    result             text        NOT NULL CHECK (result IN ('yes', 'no')),
    expiration_value   text,          -- settled value of the underlying, as Kalshi reports it (free text)
    volume             numeric,       -- lifetime contracts traded
    open_interest      numeric,
    source_tier        text        NOT NULL CHECK (source_tier IN ('historical', 'live')),
    in_cpi_set         boolean     NOT NULL DEFAULT false,
    in_calibration_set boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (venue, ticker)
);
CREATE INDEX ON markets (event_ticker);
CREATE INDEX ON markets (series_ticker);

-- Price history. `ts` is the END of the candle period (Kalshi end_period_ts), so a candle
-- with ts <= t contains only information available at time t.
CREATE TABLE candles (
    venue          text        NOT NULL DEFAULT 'kalshi',
    ticker         text        NOT NULL,
    period_minutes integer     NOT NULL,     -- 60 (CPI set) or 1440 (calibration set)
    ts             timestamptz NOT NULL,
    yes_bid        numeric,                  -- best bid at period close, dollars (0..1)
    yes_ask        numeric,                  -- best ask at period close
    mid            numeric GENERATED ALWAYS AS (
                       CASE WHEN yes_bid IS NOT NULL AND yes_ask IS NOT NULL
                            THEN (yes_bid + yes_ask) / 2 END) STORED,
    last_price     numeric,                  -- last trade at or before period close (carried if no trade)
    traded         boolean     NOT NULL,     -- any trade during this period
    volume         numeric,                  -- contracts traded during this period
    open_interest  numeric,
    PRIMARY KEY (venue, ticker, period_minutes, ts),
    FOREIGN KEY (venue, ticker) REFERENCES markets (venue, ticker)
);

-- Official CPI "truth": first release only (what Kalshi settles on).
CREATE TABLE cpi_releases (
    reference_month         date        NOT NULL,   -- first day of the month measured
    measure                 text        NOT NULL,   -- 'CPI-U SA MoM %'
    release_ts              timestamptz,            -- BLS release, 08:30 ET; NULL if never published
    first_release_value     numeric(4,1),           -- one decimal, as printed; NULL if no MoM printed
    first_release_unrounded numeric,
    index_level             numeric,
    note                    text,
    PRIMARY KEY (reference_month, measure)
);

-- Cleveland Fed daily nowcasts (point estimates, MoM %, SA, unrounded).
CREATE TABLE nowcasts (
    reference_month date        NOT NULL,
    nowcast_date    date        NOT NULL,
    nowcast_ts      timestamptz NOT NULL,   -- assumed publish time: 10:00 ET on nowcast_date
    measure         text        NOT NULL,   -- 'CPI', 'Core CPI', 'PCE', 'Core PCE'
    value           numeric     NOT NULL,
    PRIMARY KEY (reference_month, nowcast_date, measure)
);

-- Release dates of CPI and PCE data, from the Cleveland Fed chart's vertical-line markers
-- (marker 'CPI Jul' sits right after the day July CPI was published). Used to flag days
-- when a data release moved both the market and the nowcast at once.
CREATE TABLE data_release_markers (
    release_date date NOT NULL,
    marker       text NOT NULL,    -- e.g. 'CPI Jul', 'PCE Jun'
    PRIMARY KEY (release_date, marker)
);

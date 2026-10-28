"""Horizon-alignment tests for sql/views.sql, run against a throwaway schema in the local Postgres.

Builds the real schema + views in schema `test_views`, inserts a tiny hand-made release,
and checks the no-lookahead rules. Skipped if the database isn't reachable.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.ingest.http_cache import PROJECT_ROOT

ET = ZoneInfo("America/New_York")
SCHEMA = "test_views"


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


@pytest.fixture(scope="module")
def cur():
    psycopg = pytest.importorskip("psycopg")
    from src.load.load_db import connect
    try:
        conn = connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")
    conn.autocommit = True
    c = conn.cursor()
    c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}; SET search_path TO {SCHEMA}")
    c.execute((PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
    c.execute((PROJECT_ROOT / "sql" / "views.sql").read_text(encoding="utf-8"))

    release = et(2026, 7, 14, 8, 30)          # Jun 2026 CPI, first release 0.2
    last_trade = et(2026, 7, 14, 8, 29)
    c.execute("INSERT INTO cpi_releases VALUES ('2026-06-01','CPI-U SA MoM %%',%s,0.2,0.21,330,NULL)", (release,))
    # May 2026: never printed -> must be excluded from the panel
    c.execute("INSERT INTO cpi_releases VALUES ('2026-05-01','CPI-U SA MoM %%',%s,NULL,NULL,329,'x')",
              (et(2026, 6, 10, 8, 30),))
    for strike in (0.0, 0.1, 0.2, 0.3, 0.4):
        tk = f"KXCPI-26JUN-T{strike}"
        res = "yes" if 0.2 > strike else "no"   # strictly greater: 0.2 > 0.2 is False
        c.execute("""INSERT INTO markets (ticker,event_ticker,series_ticker,strike_type,bracket_low,open_time,
                     close_time,result,source_tier,in_cpi_set) VALUES (%s,'KXCPI-26JUN','KXCPI','greater',%s,
                     %s,%s,%s,'historical',true)""", (tk, strike, et(2026, 5, 1, 9), last_trade, res))
        c.execute("""INSERT INTO markets (ticker,event_ticker,series_ticker,strike_type,bracket_low,open_time,
                     close_time,result,source_tier,in_cpi_set) VALUES (%s,'KXCPI-26MAY','KXCPI','greater',%s,
                     %s,%s,'no','historical',true)""", (tk.replace("JUN", "MAY"), strike, et(2026, 4, 1, 9),
                                                       et(2026, 6, 10, 8, 29)))
    tk = "KXCPI-26JUN-T0.1"
    asof_1d = release - timedelta(days=1)                       # 2026-07-13 08:30 ET
    c.executemany("INSERT INTO candles (ticker,period_minutes,ts,yes_bid,yes_ask,last_price,traded) "
                  "VALUES (%s,60,%s,%s,%s,%s,true)", [
                      (tk, asof_1d - timedelta(minutes=30), 0.60, 0.62, 0.61),   # visible
                      (tk, asof_1d + timedelta(minutes=30), 0.90, 0.92, 0.91),   # ends after as-of: invisible
                  ])
    c.executemany("INSERT INTO nowcasts VALUES ('2026-06-01',%s,%s,'CPI',%s)", [
        (d.date(), d, v) for d, v in [
            (et(2026, 7, 9, 10), 0.15),    # Thu: last value visible at the 1-day horizon
            (et(2026, 7, 13, 10), 0.99),   # Mon 10:00 ET: published AFTER 1-day as-of (08:30) -> invisible
        ]])
    yield c
    c.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
    conn.close()


def panel(cur, horizon, ticker):
    cur.execute("SELECT yes_bid, mid, nowcast_value, nowcast_date, outcome_yes, asof_ts FROM v_cpi_horizon_panel "
                "WHERE horizon_days=%s AND ticker=%s", (horizon, ticker))
    return cur.fetchone()


def test_kalshi_quote_ignores_candles_ending_after_asof(cur):
    bid, mid, *_ = panel(cur, 1, "KXCPI-26JUN-T0.1")
    assert float(bid) == 0.60 and float(mid) == 0.61


def test_nowcast_published_after_asof_is_not_used(cur):
    _, _, value, d, *_ = panel(cur, 1, "KXCPI-26JUN-T0.1")
    assert float(value) == 0.15 and str(d) == "2026-07-09"   # carried forward, not Monday's 0.99


def test_asof_is_release_minus_horizon(cur):
    *_, asof = panel(cur, 7, "KXCPI-26JUN-T0.1")
    assert asof == et(2026, 7, 7, 8, 30)


def test_asof_capped_at_last_trade(cur):
    # 0-day isn't a panel horizon; check capping by moving last trade earlier
    cur.execute("UPDATE markets SET close_time=%s WHERE event_ticker='KXCPI-26JUN'", (et(2026, 7, 10, 16),))
    *_, asof = panel(cur, 1, "KXCPI-26JUN-T0.1")
    cur.execute("UPDATE markets SET close_time=%s WHERE event_ticker='KXCPI-26JUN'", (et(2026, 7, 14, 8, 29),))
    assert asof == et(2026, 7, 10, 16)


def test_outcome_is_strictly_greater(cur):
    assert panel(cur, 1, "KXCPI-26JUN-T0.1")[4] is True
    assert panel(cur, 1, "KXCPI-26JUN-T0.2")[4] is False      # 0.2 is not > 0.2


def test_release_without_official_print_excluded(cur):
    cur.execute("SELECT count(*) FROM v_cpi_horizon_panel WHERE event_ticker='KXCPI-26MAY'")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT in_headtohead, exclusion_reason FROM v_cpi_events WHERE event_ticker='KXCPI-26MAY'")
    assert cur.fetchone() == (False, "no official MoM print (2025 shutdown)")


def test_event_reference_month_parsed_from_ticker(cur):
    cur.execute("SELECT reference_month FROM v_cpi_events WHERE event_ticker='KXCPI-26JUN'")
    assert str(cur.fetchone()[0]) == "2026-06-01"

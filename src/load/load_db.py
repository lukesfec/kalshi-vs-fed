"""Load the raw cache (data/raw, data/processed) into Postgres.

Idempotent: drops and recreates every table (sql/schema.sql), bulk-loads with COPY, then
creates the analysis views (sql/views.sql). The database is always rebuildable from cache.

Usage:  .venv/Scripts/python -m src.load.load_db
Needs:  .env with PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
"""
from __future__ import annotations

import csv
import glob
import json
import logging
import os
import re
import time
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv

from src.ingest.http_cache import PROJECT_ROOT, RAW_DIR

log = logging.getLogger("load")

ET = ZoneInfo("America/New_York")
KALSHI_RAW = RAW_DIR / "kalshi"
PROCESSED = PROJECT_ROOT / "data" / "processed"
CPI_RELEASE_TIME_ET = dtime(8, 30)   # BLS publishes CPI at 8:30 a.m. ET
NOWCAST_TIME_ET = dtime(10, 0)       # Cleveland Fed updates "around 10:00 a.m. ET"


def connect() -> psycopg.Connection:
    load_dotenv(PROJECT_ROOT / ".env")
    return psycopg.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"], user=os.environ["PGUSER"],
                           password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"])


def et_timestamp(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(timezone.utc)


def num(x):
    """Kalshi sends numbers as strings ('0.3500', '12.00'); missing values as None/''."""
    if x is None or x == "":
        return None
    return float(x)


def pick(d: dict | None, *keys):
    """First present key. Historical and live tiers name the same field differently."""
    if not d:
        return None
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


# ---------------------------------------------------------------- markets

def load_manifests() -> dict[str, dict]:
    """ticker -> {tier, series, category, in_cpi, in_cal, period}. A market may be in both sets."""
    wanted: dict[str, dict] = {}
    for name, flag, period in (("cpi.json", "in_cpi", 60), ("calibration_seed42.json", "in_cal", 1440)):
        path = KALSHI_RAW / "manifests" / name
        if not path.exists():
            log.warning("manifest %s missing; skipping", name)
            continue
        for m in json.loads(path.read_text(encoding="utf-8"))["markets"]:
            w = wanted.setdefault(m["ticker"], {"tier": m["tier"], "series": m["series_ticker"],
                                                "category": m.get("category"), "in_cpi": False,
                                                "in_cal": False, "periods": set()})
            w[flag] = True
            w["periods"].add(period)
            w["category"] = w["category"] or m.get("category")
    return wanted


def find_market_objects(wanted: dict[str, dict]) -> dict[str, dict]:
    """Full market JSON for each wanted ticker, from the cached listing pages of its tier."""
    series = {w["series"] for w in wanted.values()}
    found: dict[str, dict] = {}
    for tier in ("historical", "live"):
        # sorted -> later cutoffs/dates overwrite earlier ones, so the newest listing wins
        for d in sorted(glob.glob(str(KALSHI_RAW / "markets" / tier / "*"))):
            for s in series:
                for f in sorted(glob.glob(os.path.join(d, s, "p*.json"))):
                    for m in json.loads(Path(f).read_text(encoding="utf-8")).get("markets", []):
                        w = wanted.get(m["ticker"])
                        if w and w["tier"] == tier:
                            found[m["ticker"]] = m
    missing = set(wanted) - set(found)
    if missing:
        raise RuntimeError(f"{len(missing)} manifest markets not found in listing cache, e.g. {sorted(missing)[:3]}")
    return found


CPI_STRIKE_TICKER = re.compile(r"-T(N?)(-?\d+(?:\.\d+)?)$")      # CPI-22AUG-TN0.4, KXCPI-26JUL-T-0.4
CPI_STRIKE_TITLE = re.compile(r"(?:more than|above|greater than) (-?\d+(?:\.\d+)?)%")


def cpi_strike(m: dict) -> tuple[str | None, float | None]:
    """(strike_type, strike) for a CPI market.

    Before Dec 2022 Kalshi left floor_strike/strike_type empty on CPI markets; the strike
    is only in the ticker ('N' prefix = negative). Every CPI contract is "CPI rises MORE
    THAN x%", i.e. strike_type 'greater'. The ticker-derived strike must agree with the title.
    """
    if m.get("floor_strike") is not None:
        return m.get("strike_type") or "greater", float(m["floor_strike"])
    tk = CPI_STRIKE_TICKER.search(m["ticker"])
    if not tk:
        raise ValueError(f"cannot parse CPI strike from {m['ticker']}")
    strike = float(tk.group(2)) * (-1 if tk.group(1) else 1)
    ti = CPI_STRIKE_TITLE.search(m.get("title") or "")
    if not ti or abs(float(ti.group(1)) - strike) > 1e-9:
        raise ValueError(f"ticker strike {strike} disagrees with title {m.get('title')!r}")
    return "greater", strike


def market_rows(wanted, found):
    for t, m in found.items():
        w = wanted[t]
        category = w["category"] or ("Economics" if w["in_cpi"] else None)
        if w["in_cpi"]:
            strike_type, strike = cpi_strike(m)
        else:
            strike_type, strike = m.get("strike_type"), num(m.get("floor_strike"))
        yield ("kalshi", t, m["event_ticker"], w["series"], category, m.get("title"), strike_type,
               strike, num(m.get("cap_strike")), m.get("open_time"), m.get("close_time"),
               m.get("settlement_ts"), m["result"], m.get("expiration_value"),
               num(pick(m, "volume_fp", "volume")), num(pick(m, "open_interest_fp", "open_interest")),
               w["tier"], w["in_cpi"], w["in_cal"])


# ---------------------------------------------------------------- candles

def candle_rows(ticker: str, period: int):
    """Normalized candles for one market, deduplicated across chunk boundaries."""
    seen = {}
    for f in sorted(glob.glob(str(KALSHI_RAW / "candles" / f"{period}m" / ticker / "*.json"))):
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        for c in data.get("candlesticks", []):
            price = c.get("price") or {}
            close_px = num(pick(price, "close", "close_dollars"))
            prev_px = num(pick(price, "previous", "previous_dollars"))
            vol = num(pick(c, "volume", "volume_fp")) or 0.0
            seen[c["end_period_ts"]] = (
                "kalshi", ticker, period, datetime.fromtimestamp(c["end_period_ts"], tz=timezone.utc),
                num(pick(c.get("yes_bid"), "close", "close_dollars")),
                num(pick(c.get("yes_ask"), "close", "close_dollars")),
                close_px if close_px is not None else prev_px,
                vol > 0, vol, num(pick(c, "open_interest", "open_interest_fp")))
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------- CPI + nowcasts

def cpi_release_rows():
    with (PROCESSED / "cpi_first_release.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rel = et_timestamp(date.fromisoformat(r["release_date"]), CPI_RELEASE_TIME_ET) if r["release_date"] else None
            yield (date.fromisoformat(r["reference_month"] + "-01"), "CPI-U SA MoM %", rel,
                   num(r["mom_pct_1dp"]), num(r["mom_pct_unrounded"]), num(r["index_level"]), r["note"] or None)


def nowcast_rows():
    with (PROCESSED / "nowcasts.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["kind"] != "nowcast":   # "actual" values come from ALFRED instead
                continue
            d = date.fromisoformat(r["nowcast_date"])
            yield (date.fromisoformat(r["reference_month"] + "-01"), d, et_timestamp(d, NOWCAST_TIME_ET),
                   r["measure"], float(r["value"]))


def marker_rows():
    """Unique (release day, marker). The same marker appears on several target months' charts."""
    seen = set()
    with (PROCESSED / "nowcast_release_markers.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["after_date"]:
                seen.add((date.fromisoformat(r["after_date"]), r["marker"]))
    yield from sorted(seen)


# ---------------------------------------------------------------- main

def copy(cur, table: str, cols: list[str], rows) -> int:
    n = 0
    with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)
            n += 1
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.time()
    wanted = load_manifests()
    log.info("manifests: %d unique markets", len(wanted))
    found = find_market_objects(wanted)
    log.info("market objects located (%.0fs)", time.time() - t0)

    with connect() as conn, conn.cursor() as cur:
        cur.execute((PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
        n = copy(cur, "markets", ["venue", "ticker", "event_ticker", "series_ticker", "category", "title",
                                  "strike_type", "bracket_low", "bracket_high", "open_time", "close_time",
                                  "settlement_time", "result", "expiration_value", "volume", "open_interest",
                                  "source_tier", "in_cpi_set", "in_calibration_set"], market_rows(wanted, found))
        log.info("markets: %d", n)

        def all_candles():
            for t, w in wanted.items():
                for period in sorted(w["periods"]):
                    yield from candle_rows(t, period)
        n = copy(cur, "candles", ["venue", "ticker", "period_minutes", "ts", "yes_bid", "yes_ask",
                                  "last_price", "traded", "volume", "open_interest"], all_candles())
        log.info("candles: %d", n)
        n = copy(cur, "cpi_releases", ["reference_month", "measure", "release_ts", "first_release_value",
                                       "first_release_unrounded", "index_level", "note"], cpi_release_rows())
        log.info("cpi_releases: %d", n)
        n = copy(cur, "nowcasts", ["reference_month", "nowcast_date", "nowcast_ts", "measure", "value"],
                 nowcast_rows())
        log.info("nowcasts: %d", n)
        n = copy(cur, "data_release_markers", ["release_date", "marker"], marker_rows())
        log.info("data_release_markers: %d", n)
        cur.execute((PROJECT_ROOT / "sql" / "views.sql").read_text(encoding="utf-8"))
        cur.execute("ANALYZE")
    log.info("done in %.0fs", time.time() - t0)


if __name__ == "__main__":
    main()

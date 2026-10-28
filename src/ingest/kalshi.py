"""Kalshi ingestion: settled markets + candlesticks, cached to data/raw/kalshi/.

Two jobs:
  cpi          All settled markets for the CPI series (default KXCPI), hourly candles.
  calibration  A seeded, stratified random sample of the whole exchange, daily candles.

Usage (from the project root):
  .venv/Scripts/python -m src.ingest.kalshi cpi
  .venv/Scripts/python -m src.ingest.kalshi calibration --series-per-category 40 --markets-per-series 50

Kalshi splits data into two tiers (see FEASIBILITY.md):
  - markets settled before GET /historical/cutoff -> /historical/markets (+ /historical/.../candlesticks)
  - everything newer                              -> /markets (+ /series/{s}/markets/{t}/candlesticks)
We list both tiers and remember which tier each market came from, so its candles are
requested from the matching endpoint.

Everything is cached, so the script is safe to interrupt and re-run.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
from datetime import date, datetime, timezone
from pathlib import Path

from src.ingest.http_cache import RAW_DIR, get_json

log = logging.getLogger("kalshi")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_RAW = RAW_DIR / "kalshi"
MAX_CANDLES_PER_REQUEST = 5000  # API rejects larger windows (verified in Phase 0)
CHUNK_CANDLES = 4800            # stay safely under the limit
SETTLED_RESULTS = {"yes", "no"}


# ---------------------------------------------------------------- helpers

def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def volume_of(m: dict) -> float:
    """Historical and live tiers name the volume field differently."""
    for key in ("volume_fp", "volume"):
        if m.get(key) not in (None, ""):
            return float(m[key])
    return 0.0


def get_cutoff() -> dict:
    """Live/historical cutoff, cached per calendar day (it moves forward over time)."""
    path = KALSHI_RAW / "cutoff" / f"{date.today():%Y%m%d}.json"
    return get_json(f"{BASE}/historical/cutoff", path)


# ---------------------------------------------------------------- market listings

def _paginate(endpoint: str, params: dict, cache_dir: Path) -> list[dict]:
    out, cursor, page = [], "", 0
    while True:
        q = {**params, "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        data = get_json(f"{BASE}{endpoint}", cache_dir / f"p{page:04d}.json", params=q)
        if "__http_status" in data:
            raise RuntimeError(f"{endpoint} {params} -> HTTP {data['__http_status']}: {data['__body'][:200]}")
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        page += 1
        if not cursor:
            return out


def list_settled_markets(series: str, cutoff: dict) -> list[dict]:
    """All settled markets for a series across both tiers, each tagged with _tier and _series."""
    # The historical tier only changes when the cutoff moves, so key its cache by cutoff date.
    # The live tier changes daily, so key it by today's date.
    cutoff_tag = cutoff["market_settled_ts"][:10]
    hist = _paginate("/historical/markets", {"series_ticker": series},
                     KALSHI_RAW / "markets" / "historical" / f"cutoff_{cutoff_tag}" / series)
    live = _paginate("/markets", {"series_ticker": series, "status": "settled"},
                     KALSHI_RAW / "markets" / "live" / f"{date.today():%Y%m%d}" / series)
    seen, out = set(), []
    for tier, markets in (("historical", hist), ("live", live)):
        for m in markets:
            if m["ticker"] in seen or m.get("result") not in SETTLED_RESULTS:
                continue
            seen.add(m["ticker"])
            out.append({**m, "_tier": tier, "_series": series})
    return out


# ---------------------------------------------------------------- candlesticks

def fetch_candles(m: dict, period_minutes: int) -> int:
    """Download candles over the market's whole life, chunked. Returns number of candles."""
    start = int(parse_ts(m["open_time"]).timestamp())
    end = int(parse_ts(m["close_time"]).timestamp()) + period_minutes * 60
    if m["_tier"] == "historical":
        url = f"{BASE}/historical/markets/{m['ticker']}/candlesticks"
    else:
        url = f"{BASE}/series/{m['_series']}/markets/{m['ticker']}/candlesticks"
    cache_dir = KALSHI_RAW / "candles" / f"{period_minutes}m" / m["ticker"]
    step = CHUNK_CANDLES * period_minutes * 60
    n = 0
    for chunk_start in range(start, end, step):
        chunk_end = min(chunk_start + step, end)
        data = get_json(url, cache_dir / f"{chunk_start}_{chunk_end}.json",
                        params={"start_ts": chunk_start, "end_ts": chunk_end, "period_interval": period_minutes})
        if "__http_status" in data:
            log.warning("candles %s HTTP %s: %s", m["ticker"], data["__http_status"], data["__body"][:150])
            continue
        n += len(data.get("candlesticks", []))
    return n


def write_manifest(name: str, markets: list[dict], extra: dict) -> Path:
    """Record exactly which markets this run covered (for the loader and for reproducibility)."""
    path = KALSHI_RAW / "manifests" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"ticker": m["ticker"], "event_ticker": m["event_ticker"], "series_ticker": m["_series"],
             "category": m.get("_category"), "tier": m["_tier"], "volume": volume_of(m)} for m in markets]
    path.write_text(json.dumps({**extra, "created": datetime.now(timezone.utc).isoformat(),
                                "n_markets": len(rows), "markets": rows}, indent=1), encoding="utf-8")
    return path


# ---------------------------------------------------------------- jobs

def run_cpi(series_list: list[str], period_minutes: int) -> None:
    cutoff = get_cutoff()
    log.info("cutoff: %s", cutoff)
    all_markets = []
    for series in series_list:
        markets = list_settled_markets(series, cutoff)
        events = {m["event_ticker"] for m in markets}
        log.info("%s: %d settled markets across %d events", series, len(markets), len(events))
        for i, m in enumerate(markets, 1):
            n = fetch_candles(m, period_minutes)
            if i % 50 == 0 or i == len(markets):
                log.info("  candles %d/%d (last: %s, %d candles)", i, len(markets), m["ticker"], n)
        all_markets += markets
    path = write_manifest("cpi", all_markets, {"series": series_list, "period_minutes": period_minutes,
                                               "cutoff": cutoff})
    log.info("manifest -> %s", path.relative_to(RAW_DIR.parent.parent))


def is_combo_series(s: dict) -> bool:
    """Auto-generated multi-leg combo/parlay series (tickers KXMVE...), excluded from calibration."""
    return s["ticker"].upper().startswith("KXMVE") or "multivariate" in json.dumps(s.get("tags") or []).lower()


def run_calibration(series_per_category: int, markets_per_series: int, seed: int, period_minutes: int) -> None:
    cutoff = get_cutoff()
    # One frozen snapshot of the series list keeps the sample reproducible across re-runs.
    series_all = get_json(f"{BASE}/series", KALSHI_RAW / "series" / "all_series_snapshot.json")["series"]
    rng = random.Random(seed)

    by_cat: dict[str, list[dict]] = {}
    for s in sorted(series_all, key=lambda x: x["ticker"]):
        if not is_combo_series(s):
            by_cat.setdefault(s.get("category") or "Unknown", []).append(s)
    sampled = []
    for cat in sorted(by_cat):
        pool = by_cat[cat][:]
        rng.shuffle(pool)
        sampled += [(cat, s["ticker"]) for s in pool[:series_per_category]]
    log.info("sampled %d series across %d categories (seed=%d)", len(sampled), len(by_cat), seed)

    chosen = []
    for i, (cat, series) in enumerate(sampled, 1):
        markets = [m for m in list_settled_markets(series, cutoff)
                   if m.get("market_type", "binary") == "binary" and volume_of(m) > 0]
        markets.sort(key=lambda m: m["ticker"])
        rng_series = random.Random(f"{seed}-{series}")  # per-series RNG: adding series never reshuffles others
        if len(markets) > markets_per_series:
            markets = rng_series.sample(markets, markets_per_series)
        for m in markets:
            m["_category"] = cat
        chosen += markets
        if i % 25 == 0 or i == len(sampled):
            log.info("listed %d/%d series; %d markets selected so far", i, len(sampled), len(chosen))

    write_manifest(f"calibration_seed{seed}", chosen,
                   {"seed": seed, "series_per_category": series_per_category,
                    "markets_per_series": markets_per_series, "period_minutes": period_minutes,
                    "cutoff": cutoff, "n_series_sampled": len(sampled)})
    for i, m in enumerate(chosen, 1):
        fetch_candles(m, period_minutes)
        if i % 500 == 0 or i == len(chosen):
            log.info("candles %d/%d", i, len(chosen))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="job", required=True)
    p_cpi = sub.add_parser("cpi")
    p_cpi.add_argument("--series", nargs="+", default=["KXCPI"])
    p_cpi.add_argument("--period", type=int, default=60, help="candle minutes: 1, 60, or 1440")
    p_cal = sub.add_parser("calibration")
    p_cal.add_argument("--series-per-category", type=int, default=40)
    p_cal.add_argument("--markets-per-series", type=int, default=50)
    p_cal.add_argument("--seed", type=int, default=42)
    p_cal.add_argument("--period", type=int, default=1440)
    args = ap.parse_args()
    if args.job == "cpi":
        run_cpi(args.series, args.period)
    else:
        run_calibration(args.series_per_category, args.markets_per_series, args.seed, args.period)


if __name__ == "__main__":
    main()

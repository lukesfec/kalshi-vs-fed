"""First-release CPI from ALFRED (St. Louis Fed real-time archive), no API key needed.

Kalshi's CPI contracts settle on the FIRST published one-month % change in seasonally
adjusted CPI-U (FRED series CPIAUCSL), rounded to one decimal; later revisions are ignored.
So for each reference month we need the data exactly as it looked on release day.

How we get it without an API key:
  ALFRED serves any past vintage as CSV:  alfredgraph.csv?id=CPIAUCSL&vintage_date=YYYY-MM-DD
  A month's value appears only in vintages dated on/after its release. So for each month we
  binary-search the release date (about 5 requests), and the vintage on that date gives the
  first-release index levels. MoM is computed within that one vintage, which is what BLS printed.

Usage:  .venv/Scripts/python -m src.ingest.fred [--start 2013-06] [--series CPIAUCSL]
Output: data/raw/fred/alfred/<series>_<vintage>.csv   (raw cache)
        data/processed/cpi_first_release.csv          (one row per reference month)
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from src.ingest.http_cache import PROJECT_ROOT, RAW_DIR, get_text

log = logging.getLogger("fred")

ALFRED_CSV = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
OUT_CSV = PROJECT_ROOT / "data" / "processed" / "cpi_first_release.csv"


def month_add(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def vintage(series: str, vdate: date) -> dict[date, float | None]:
    """Series as it was published on vdate: {observation month -> index level}."""
    text = get_text(ALFRED_CSV, RAW_DIR / "fred" / "alfred" / f"{series}_{vdate:%Y%m%d}.csv",
                    params={"id": series, "vintage_date": vdate.isoformat()})
    rows = csv.reader(io.StringIO(text))
    next(rows)  # header
    out = {}
    for obs, val in rows:
        out[date.fromisoformat(obs)] = float(val) if val not in ("", ".") else None
    return out


def has_month(series: str, vdate: date, month: date) -> bool:
    return vintage(series, vdate).get(month) is not None


def find_release_date(series: str, month: date) -> date | None:
    """Earliest vintage date containing `month`, searched over the following two months.

    Returns None if the month was never published in that window (e.g. Oct 2025, skipped
    because of the federal government shutdown).
    """
    lo, hi = month_add(month, 1), month_add(month, 3) - timedelta(days=1)
    if hi > date.today():
        hi = date.today()
    if lo > hi or not has_month(series, hi, month):
        return None
    while lo < hi:  # invariant: month is present at hi
        mid = lo + (hi - lo) // 2
        if has_month(series, mid, month):
            hi = mid
        else:
            lo = mid + timedelta(days=1)
    return lo


def round_1dp(x: float) -> float:
    """BLS-style rounding to one decimal (half away from zero), avoiding binary-float surprises."""
    return float(Decimal(repr(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="CPIAUCSL")
    ap.add_argument("--start", default="2013-06", help="first reference month (YYYY-MM)")
    args = ap.parse_args()

    # Latest vintage too, for reference (revised values; NOT used as truth).
    get_text(FRED_CSV, RAW_DIR / "fred" / f"{args.series}_current_{date.today():%Y%m%d}.csv",
             params={"id": args.series})

    month = date.fromisoformat(args.start + "-01")
    rows = []
    while month_add(month, 1) <= date.today():
        rel = find_release_date(args.series, month)
        if rel is None:
            log.warning("%s: no release found (never published?)", f"{month:%Y-%m}")
            rows.append({"reference_month": f"{month:%Y-%m}", "release_date": "", "index_level": "",
                         "prev_index_level": "", "mom_pct_unrounded": "", "mom_pct_1dp": "",
                         "note": "not published within 2 months"})
        else:
            v = vintage(args.series, rel)
            cur, prev = v.get(month), v.get(month_add(month, -1))
            note = ""
            if prev is None:
                # Happens when the prior month was never published (Nov 2025 after the Oct 2025 gap).
                mom = None
                note = "previous month missing in first-release vintage; no MoM published"
            else:
                mom = (cur / prev - 1) * 100
            rows.append({"reference_month": f"{month:%Y-%m}", "release_date": rel.isoformat(),
                         "index_level": cur, "prev_index_level": prev if prev is not None else "",
                         "mom_pct_unrounded": f"{mom:.6f}" if mom is not None else "",
                         "mom_pct_1dp": round_1dp(mom) if mom is not None else "", "note": note})
            log.info("%s released %s  MoM %s", f"{month:%Y-%m}", rel, rows[-1]["mom_pct_1dp"])
        month = month_add(month, 1)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    log.info("wrote %d rows -> %s", len(rows), OUT_CSV.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    main()

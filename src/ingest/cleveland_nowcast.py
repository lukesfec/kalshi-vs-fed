"""Cleveland Fed inflation nowcasts: daily nowcast sequence for every target month.

Source: the JSON file behind the charting tool on
https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting
(found in Phase 0; public, no key). One chart object per target month, each with daily
nowcasts for CPI, Core CPI, PCE, Core PCE (MoM %, SA, unrounded) plus the "actual" values.

The file is snapshotted once per calendar day (data/raw/cleveland/nowcast_month_YYYYMMDD.json).
Keeping dated snapshots lets us later verify that past values are never revised.

Usage:  .venv/Scripts/python -m src.ingest.cleveland_nowcast
Output: data/processed/nowcasts.csv  (reference_month, nowcast_date, measure, kind, value)
"""
from __future__ import annotations

import csv
import logging
from datetime import date

from src.ingest.http_cache import PROJECT_ROOT, RAW_DIR, get_json

log = logging.getLogger("cleveland")

URL = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json"
OUT_CSV = PROJECT_ROOT / "data" / "processed" / "nowcasts.csv"
MARKERS_CSV = PROJECT_ROOT / "data" / "processed" / "nowcast_release_markers.csv"

# chart series name -> (measure, kind)
SERIES = {
    "CPI Inflation": ("CPI", "nowcast"),
    "Core CPI Inflation": ("Core CPI", "nowcast"),
    "PCE Inflation": ("PCE", "nowcast"),
    "Core PCE Inflation": ("Core PCE", "nowcast"),
    "Actual CPI Inflation": ("CPI", "actual"),
    "Actual Core CPI Inflation": ("Core CPI", "actual"),
    "Actual PCE Inflation": ("PCE", "actual"),
    "Actual Core PCE Inflation": ("Core PCE", "actual"),
}


def label_to_date(label: str, target_year: int, target_month: int) -> date:
    """Chart labels are 'MM/DD' without a year. Nowcasting for a target month starts in that
    month and runs into the following months, so a label month earlier than the target month
    means the calendar wrapped into the next year (e.g. target Dec 2024, label 01/10 -> 2025)."""
    mm, dd = (int(x) for x in label.split("/"))
    year = target_year + 1 if mm < target_month else target_year
    return date(year, mm, dd)


def parse(charts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (data rows, release-marker rows).

    Besides 'MM/DD' dates, the category axis contains vertical-line markers such as
    'CPI Jul' (vline: true) drawn right after the day a CPI/PCE release came out. They are
    not observations; we keep them separately as an independent record of release dates.
    """
    rows, markers = [], []
    for ch in charts:
        y, m = (int(x) for x in ch["chart"]["subcaption"].split("-"))
        cats = ch["categories"][0]["category"]
        dates: list[date | None] = []
        last_date = None
        for c in cats:
            if c.get("vline") in ("true", True):
                dates.append(None)
                markers.append({"reference_month": f"{y}-{m:02d}", "marker": c["label"],
                                "after_date": last_date.isoformat() if last_date else ""})
            else:
                last_date = label_to_date(c["label"], y, m)
                dates.append(last_date)
        real = [d for d in dates if d is not None]
        if real != sorted(real):
            raise ValueError(f"non-monotonic dates for {y}-{m:02d}: year inference failed")
        for ds in ch["dataset"]:
            if ds["seriesname"] not in SERIES:
                log.warning("unknown series %r in %d-%02d", ds["seriesname"], y, m)
                continue
            measure, kind = SERIES[ds["seriesname"]]
            for d, point in zip(dates, ds["data"]):
                v = point.get("value")
                if d is None or v in (None, ""):
                    continue
                rows.append({"reference_month": f"{y}-{m:02d}", "nowcast_date": d.isoformat(),
                             "measure": measure, "kind": kind, "value": float(v)})
    return rows, markers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    snap = RAW_DIR / "cleveland" / f"nowcast_month_{date.today():%Y%m%d}.json"
    charts = get_json(URL, snap, min_interval=1.0)
    if isinstance(charts, dict) and "__http_status" in charts:
        raise SystemExit(f"download failed: HTTP {charts['__http_status']}")
    log.info("snapshot %s: %d target months (%s .. %s), file stamp %s", snap.name, len(charts),
             charts[0]["chart"]["subcaption"], charts[-1]["chart"]["subcaption"],
             charts[0]["chart"].get("_comment"))
    rows, markers = parse(charts)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["reference_month", "nowcast_date", "measure", "kind", "value"])
        w.writeheader()
        w.writerows(rows)
    with MARKERS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["reference_month", "marker", "after_date"])
        w.writeheader()
        w.writerows(markers)
    n_cpi = sum(r["measure"] == "CPI" and r["kind"] == "nowcast" for r in rows)
    log.info("wrote %d rows (%d headline-CPI nowcasts) -> %s", len(rows), n_cpi, OUT_CSV.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    main()

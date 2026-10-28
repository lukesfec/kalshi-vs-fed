"""Phase 1 cross-source validation. Prints a report; changes nothing.

Checks
  1. Truth: Kalshi's settled expiration_value per CPI event vs ALFRED first-release MoM (1dp).
     Also checks every market's yes/no result is consistent with that value ("strictly greater").
  2. Release dates: Kalshi close date vs ALFRED release date vs the Cleveland Fed's release markers.
  3. Nowcast stability: every snapshot of the nowcast file must agree on every value they share
     (evidence that past nowcasts are never re-estimated, i.e. no lookahead in the archive).

Usage:  .venv/Scripts/python -m src.ingest.validate
"""
from __future__ import annotations

import glob
import json
import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.ingest.http_cache import PROJECT_ROOT, RAW_DIR

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                      "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
ET = ZoneInfo("America/New_York")


def to_num(x) -> float | None:
    """Kalshi's expiration_value is free text in older events ('.9%', '0.1'); ALFRED blanks are NaN."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip().rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return None


def event_reference_month(event_ticker: str) -> str:
    m = re.search(r"-(\d\d)([A-Z]{3})$", event_ticker)
    return f"20{m.group(1)}-{MONTHS[m.group(2)]:02d}"


def load_cpi_markets() -> list[dict]:
    """Full market objects for the tickers in the CPI manifest (from the cached listings)."""
    manifest = json.loads((RAW_DIR / "kalshi" / "manifests" / "cpi.json").read_text())
    wanted = {m["ticker"] for m in manifest["markets"]}
    found = {}
    for f in glob.glob(str(RAW_DIR / "kalshi" / "markets" / "*" / "*" / "KXCPI" / "*.json")):
        for m in json.loads(open(f, encoding="utf-8").read()).get("markets", []):
            if m["ticker"] in wanted:
                found[m["ticker"]] = m
    return list(found.values())


def check_truth_and_dates() -> None:
    markets = load_cpi_markets()
    alfred = pd.read_csv(PROJECT_ROOT / "data" / "processed" / "cpi_first_release.csv", dtype=str)
    alfred = alfred.set_index("reference_month")
    markers = pd.read_csv(PROJECT_ROOT / "data" / "processed" / "nowcast_release_markers.csv", dtype=str)
    cpi_markers = markers[markers.marker.str.startswith("CPI ")].drop_duplicates(["marker", "after_date"])
    marker_dates = defaultdict(set)
    for _, r in cpi_markers.iterrows():
        marker_dates[r.marker].add(r.after_date)

    by_event = defaultdict(list)
    for m in markets:
        by_event[m["event_ticker"]].append(m)

    rows = []
    for ev, ms in sorted(by_event.items(), key=lambda kv: event_reference_month(kv[0])):
        ref = event_reference_month(ev)
        exp_vals = {to_num(m.get("expiration_value")) for m in ms}
        exp = next(iter(exp_vals)) if len(exp_vals) == 1 else None
        raw_exp = sorted({str(m.get("expiration_value")) for m in ms})
        # every market must resolve consistently with "value strictly greater than strike"
        bad_results = []
        if exp is None:
            bad_results.append("missing/inconsistent expiration_value")
        else:
            for m in ms:
                strike = m.get("floor_strike")
                if strike is None:
                    continue
                expect = "yes" if exp > float(strike) + 1e-9 else "no"
                if m["result"] != expect:
                    bad_results.append(m["ticker"])
        close_et = min(datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) for m in ms).astimezone(ET)
        a = alfred.loc[ref] if ref in alfred.index else None
        alfred_val = to_num(a.mom_pct_1dp) if a is not None else None
        alfred_rel = a.release_date if a is not None and isinstance(a.release_date, str) else ""
        mon_label = "CPI " + datetime.strptime(ref, "%Y-%m").strftime("%b")
        mk = sorted(d for d in marker_dates.get(mon_label, set()) if d[:7] > ref)[:1]
        rows.append({
            "event": ev, "ref": ref, "n_mkts": len(ms), "kalshi_value": exp,
            "kalshi_raw": "|".join(raw_exp), "alfred_1dp": alfred_val,
            "value_match": exp is not None and alfred_val is not None and abs(exp - alfred_val) < 1e-9,
            "results_consistent": not bad_results,
            "kalshi_close_et": close_et.date().isoformat(), "alfred_release": alfred_rel,
            "fed_marker": mk[0] if mk else "",
        })
    df = pd.DataFrame(rows)
    df["date_match_alfred"] = df.kalshi_close_et == df.alfred_release
    df["date_match_marker"] = df.kalshi_close_et == df.fed_marker

    print("=== 1. Truth: Kalshi settlement vs ALFRED first release")
    print(f"events: {len(df)} | value match: {df.value_match.sum()} | "
          f"results consistent with value: {df.results_consistent.sum()}")
    bad = df[~df.value_match | ~df.results_consistent]
    if len(bad):
        print("mismatches / anomalies:")
        print(bad[["event", "kalshi_raw", "kalshi_value", "alfred_1dp", "results_consistent",
                   "alfred_release"]].to_string(index=False))
    print("\n=== 2. Release dates: Kalshi close (ET) vs ALFRED vs Cleveland marker")
    print(f"match ALFRED: {df.date_match_alfred.sum()}/{len(df)} | match Fed marker: {df.date_match_marker.sum()}/{len(df)}")
    dbad = df[~df.date_match_alfred | ~df.date_match_marker]
    if len(dbad):
        print(dbad[["event", "kalshi_close_et", "alfred_release", "fed_marker"]].to_string(index=False))
    out = PROJECT_ROOT / "data" / "processed" / "validation_cpi_events.csv"
    df.to_csv(out, index=False)
    print(f"(full table -> {out.relative_to(PROJECT_ROOT)})")


def check_nowcast_stability() -> None:
    print("\n=== 3. Nowcast stability across snapshots")
    snaps = sorted(glob.glob(str(RAW_DIR / "cleveland" / "nowcast_month_*.json")) +
                   glob.glob(str(RAW_DIR / "phase0" / "cleveland" / "wayback_*.json")))

    def series(path: str) -> dict:
        out = {}
        for ch in json.loads(open(path, encoding="utf-8").read()):
            tm = ch["chart"]["subcaption"]
            labels = [c["label"] for c in ch["categories"][0]["category"]]
            for ds in ch["dataset"]:
                for lab, p in zip(labels, ds["data"]):
                    if p.get("value") not in (None, ""):
                        out[(tm, ds["seriesname"], lab)] = p["value"]
        return out

    loaded = {p: series(p) for p in snaps}
    names = {p: re.sub(r".*[\\/]", "", p) for p in snaps}
    base = snaps[-1]
    for p in snaps[:-1]:
        common = loaded[p].keys() & loaded[base].keys()
        diff = sum(loaded[p][k] != loaded[base][k] for k in common)
        print(f"{names[p]} vs {names[base]}: {len(common)} shared values, {diff} differ")
    if len(snaps) < 2:
        print("only one snapshot available")


if __name__ == "__main__":
    check_truth_and_dates()
    check_nowcast_stability()

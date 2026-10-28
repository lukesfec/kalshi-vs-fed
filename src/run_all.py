"""Rebuild everything downstream of the raw cache, in order.

Usage:  .venv/Scripts/python -m src.run_all [--skip-load]
  1. load the raw cache into Postgres (sql/schema.sql + sql/views.sql)
  2. Phase 3 calibration, 4 head-to-head, 5 lead-lag, 6 backtest   -> outputs/phase*/
  3. build the static dashboard                                     -> docs/index.html
Data downloads (src.ingest.*) are separate, since they hit external APIs; see README.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

STEPS = [
    ("load database", ["src.load.load_db"]),
    ("phase 3 calibration", ["src.analysis.calibration"]),
    ("phase 4 head-to-head", ["src.analysis.cpi_headtohead"]),
    ("phase 5 lead-lag", ["src.analysis.leadlag"]),
    ("phase 6 backtest", ["src.analysis.backtest"]),
    ("dashboard", ["src.viz.build_dashboard"]),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-load", action="store_true", help="reuse the current database")
    args = ap.parse_args()
    for name, mod in STEPS:
        if args.skip_load and name == "load database":
            continue
        t0 = time.time()
        print(f"==> {name}", flush=True)
        r = subprocess.run([sys.executable, "-m", *mod], capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-3000:], r.stderr[-3000:], sep="\n")
            raise SystemExit(f"step failed: {name}")
        print(f"    ok ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()

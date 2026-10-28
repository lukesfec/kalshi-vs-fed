"""Build the static dashboard (docs/index.html) for GitHub Pages.

Usage:  .venv/Scripts/python -m src.viz.build_dashboard
Reads:  outputs/phase3..6 (written by the analysis scripts from Postgres), data/processed paths,
        and release facts straight from Postgres.
Writes: docs/index.html, docs/assets/plotly.min.js, docs/data/*.csv

Every number on the page is inserted here from those files; nothing is typed by hand.
Charts are drawn in the browser by Plotly.js (bundled locally, no CDN) so they can re-theme
for light/dark mode.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import plotly

from src.analysis.cpi_headtohead import nw_lag
from src.analysis.db import OUTPUT_DIR, query
from src.analysis.scoring import block_bootstrap_mean_ci
from src.ingest.http_cache import PROJECT_ROOT

DOCS = PROJECT_ROOT / "docs"
TEMPLATE = Path(__file__).with_name("template.html")
P3, P4, P5, P6 = (OUTPUT_DIR / f"phase{i}" for i in (3, 4, 5, 6))


def boot_mean_ci(v: np.ndarray, n_boot: int = 2000, seed: int = 1) -> tuple[float, float]:
    """Same circular block bootstrap as the head-to-head tests (v must be in time order)."""
    return block_bootstrap_mean_ci(v, block=nw_lag(len(v)), n_boot=n_boot, seed=seed)


def month_label(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %Y")


def records(df: pd.DataFrame, cols: list[str], digits: int = 5) -> list[dict]:
    out = df[cols].copy()
    for c in out.select_dtypes("number"):
        out[c] = out[c].round(digits)
    return json.loads(out.to_json(orient="records", date_format="iso"))


def missing_at_30_days(scores: pd.DataFrame) -> list[dict]:
    """Releases with no 30-day score, and how long before release their first market opened."""
    miss = sorted(set(scores.event_ticker) - set(scores[scores.horizon_days == 30].event_ticker))
    if not miss:
        return []
    ev = query("SELECT event_ticker, reference_month, first_open_ts, release_ts FROM v_cpi_events")
    ev = ev[ev.event_ticker.isin(miss)]
    out = []
    for r in ev.itertuples():
        opened = pd.Timestamp(r.first_open_ts).tz_convert("America/New_York")
        out.append({"label": month_label(r.reference_month),
                    "days_listed": round((r.release_ts - r.first_open_ts).total_seconds() / 86400, 1),
                    "opened": f"{opened:%b} {opened.day}, {opened.year}"})
    return out


def headline() -> dict:
    s = pd.read_csv(P4 / "scores_per_release.csv").sort_values("reference_month")
    rows = []
    for h, g in s.groupby("horizon_days"):
        r = {"h": int(h), "n": len(g)}
        for src in ("kalshi", "fed", "combo"):
            v = g[f"brier_{src}"].to_numpy()
            lo, hi = boot_mean_ci(v)
            r[src] = {"mean": float(v.mean()), "lo": lo, "hi": hi}
        rows.append(r)
    dm = pd.read_csv(P4 / "diebold_mariano.csv")
    dm = dm[dm.metric == "brier"]
    enc = pd.read_csv(P4 / "encompassing.csv")
    sens = pd.read_csv(P4 / "sensitivity_normal_errors.csv")
    return {"by_horizon": rows,
            "dm": records(dm, ["horizon_days", "a", "b", "T", "mean_diff", "diff_ci_low", "diff_ci_high", "dm_stat",
                               "p_value", "share_first_better", "nw_lag"]),
            "missing_30d": missing_at_30_days(s),
            "dm_log": records(pd.read_csv(P4 / "diebold_mariano.csv").query("metric == 'log' and a == 'kalshi'"),
                              ["horizon_days", "p_value"]),
            "dm_rps": records(pd.read_csv(P4 / "diebold_mariano.csv").query("metric == 'rps' and a == 'kalshi'"),
                              ["horizon_days", "p_value"]),
            "encompassing": records(enc, ["horizon_days", "n_releases", "logit_kalshi_coef", "logit_kalshi_p",
                                          "logit_fed_coef", "logit_fed_p"]),
            "sensitivity": records(sens, ["horizon_days", "mean_diff", "p_value"])}


def releases() -> dict:
    ev = query("SELECT event_ticker, reference_month, release_ts, first_release_value, last_trade_ts "
               "FROM v_cpi_events WHERE in_headtohead ORDER BY reference_month")
    rel = query("SELECT reference_month, first_release_unrounded FROM cpi_releases WHERE measure = 'CPI-U SA MoM %%'")
    ev = ev.merge(rel, on="reference_month", how="left")
    probs = pd.read_csv(PROJECT_ROOT / "data" / "processed" / "cpi_probability_paths.csv")
    daily = pd.read_csv(PROJECT_ROOT / "data" / "processed" / "cpi_daily_paths.csv", parse_dates=["date"])
    scores = pd.read_csv(P4 / "scores_per_release.csv")
    out = []
    for _, e in ev.iterrows():
        rel_date = pd.Timestamp(e.release_ts).tz_convert("America/New_York").tz_localize(None).normalize()
        d = daily[daily.event_ticker == e.event_ticker].sort_values("date")
        p = probs[probs.event_ticker == e.event_ticker]
        sc = scores[scores.event_ticker == e.event_ticker]
        out.append({
            "id": e.event_ticker, "label": month_label(e.reference_month),
            "release": rel_date.strftime("%Y-%m-%d"), "printed": float(e.first_release_value),
            "unrounded": float(e.first_release_unrounded),
            "daily": {"days_before": [int((rel_date - x).days) for x in d.date],
                      "date": [x.strftime("%Y-%m-%d") for x in d.date],
                      "kalshi": [None if pd.isna(v) else round(float(v), 4) for v in d.k_median],
                      "fed": [round(float(v), 4) for v in d.nowcast]},
            "curves": {str(h): {"strike": g.sort_values("strike").strike.round(2).tolist(),
                                "kalshi": g.sort_values("strike").p_kalshi.round(4).tolist(),
                                "fed": g.sort_values("strike").p_fed.round(4).tolist()}
                       for h, g in p.groupby("horizon_days")},
            "brier": {str(int(r.horizon_days)): {"kalshi": round(r.brier_kalshi, 4), "fed": round(r.brier_fed, 4)}
                      for r in sc.itertuples()},
        })
    return {"list": out}


def calibration() -> dict:
    rel = pd.read_csv(P3 / "reliability_pooled.csv")
    w = pd.read_csv(P3 / "weighting_fit_pooled.csv").set_index("form")
    g = pd.read_csv(P3 / "weighting_fit_by_group.csv")
    reg = pd.read_csv(P3 / "calibration_regression.csv")
    sc = pd.read_csv(P3 / "scores_overall.csv").iloc[0]
    summ = json.loads((P3 / "summary.json").read_text())
    mm = pd.read_csv(P3 / "mispricing_model.csv", index_col=0)
    lit = pd.read_csv(P3 / "weighting_literature.csv")
    return {"reliability": records(rel, ["mean_price", "observed_freq", "ci_low", "ci_high", "n", "n_events", "bin"]),
            "tk": w.loc["tk"][["estimate", "ci_low", "ci_high"]].to_dict(),
            "prelec": w.loc["prelec"][["estimate", "ci_low", "ci_high"]].to_dict(),
            "groups": records(g, ["kind", "level", "estimate", "ci_low", "ci_high", "n_events"]),
            "regression": records(reg, ["sample", "n", "n_events", "intercept", "slope", "slope_se", "z_slope_eq_1", "joint_p"]),
            "scores": {k: float(sc[k]) for k in ("brier", "brier_ci_low", "brier_ci_high", "log_loss",
                                                 "brier_skill_vs_base_rate", "reliability", "n_points", "n_events")},
            "sample": summ["sample"], "heterogeneity": summ["heterogeneity"],
            "liq_interaction": {"coef": float(mm.loc["lp:vol_z", "coef"]), "p": float(mm.loc["lp:vol_z", "p"])},
            "literature": records(lit, ["source", "form", "value"])}


def leadlag() -> dict:
    c = pd.read_csv(P5 / "cross_correlations.csv")
    gs = pd.read_csv(P5 / "granger_summary.csv")
    desc = pd.read_csv(P5 / "descriptives.csv").iloc[0].to_dict()
    return {"ccf": records(c[c.variant.str.startswith("primary")], ["lag", "corr", "ci_low", "ci_high"]),
            "ccf_no_release_days": records(c[c.variant.str.startswith("excluding")], ["lag", "corr", "ci_low", "ci_high"]),
            "granger": json.loads(gs.to_json(orient="records")), "desc": desc}


def backtest() -> dict:
    sw = pd.read_csv(P6 / "threshold_sweep.csv")
    res = pd.read_csv(P6 / "test_result.csv")
    net = sw[(sw.cost_model == "bid/ask + fees") & (sw.n_trades > 0)]
    theta = float(res.theta.iloc[0])
    train_at = sw[(sw.period == "train") & (sw.theta == theta)]
    return {"sweep": {p: records(net[net.period == p], ["theta", "mean_pnl_cents", "ci_low_cents", "ci_high_cents",
                                                         "n_trades", "n_releases"]) for p in ("train", "test")},
            "theta": theta,
            "test": records(res, ["cost_model", "n_trades", "n_releases", "mean_pnl_cents", "ci_low_cents",
                                  "ci_high_cents", "hit_rate", "mean_fee_cents", "t_stat_release",
                                  "min_detectable_edge_cents"]),
            "train_at_theta": records(train_at, ["cost_model", "n_trades", "n_releases", "mean_pnl_cents",
                                                 "ci_low_cents", "ci_high_cents", "t_stat_release"]),
            "train_best_positive": bool(net[net.period == "train"].query("n_trades >= 20").total_pnl_per_100.max() > 0),
            "train_profitable": int((net[net.period == "train"].total_pnl_per_100 > 0).sum()),
            "n_thresholds": int(net[net.period == "train"].theta.nunique()),
            "split": {p: int(sw[(sw.period == p) & (sw.theta == 0) & (sw.cost_model == "mid, no fees")].n_releases.iloc[0])
                      for p in ("train", "test")}}


def main() -> None:
    data = {"headline": headline(), "releases": releases(), "calibration": calibration(),
            "leadlag": leadlag(), "backtest": backtest()}
    n_rel = len(data["releases"]["list"])
    data["meta"] = {"n_releases": n_rel, "first": data["releases"]["list"][0]["label"],
                    "last": data["releases"]["list"][-1]["label"],
                    "built": pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")}

    DOCS.mkdir(exist_ok=True)
    (DOCS / "assets").mkdir(exist_ok=True)
    (DOCS / "data").mkdir(exist_ok=True)
    shutil.copy(Path(plotly.__file__).parent / "package_data" / "plotly.min.js", DOCS / "assets" / "plotly.min.js")
    # downloadable tables (also the accessible "table view" of each chart)
    for src, name in ((P4 / "scores_per_release.csv", "headtohead_scores_per_release.csv"),
                      (P4 / "diebold_mariano.csv", "headtohead_diebold_mariano.csv"),
                      (P4 / "encompassing.csv", "headtohead_encompassing.csv"),
                      (PROJECT_ROOT / "data" / "processed" / "cpi_probability_paths.csv", "cpi_probability_paths.csv"),
                      (PROJECT_ROOT / "data" / "processed" / "cpi_daily_paths.csv", "cpi_daily_paths.csv"),
                      (P3 / "reliability_pooled.csv", "calibration_reliability.csv"),
                      (P3 / "weighting_fit_by_group.csv", "calibration_weighting_by_group.csv"),
                      (P5 / "cross_correlations.csv", "leadlag_cross_correlations.csv"),
                      (P6 / "threshold_sweep.csv", "backtest_threshold_sweep.csv")):
        shutil.copy(src, DOCS / "data" / name)

    payload = json.dumps(data, separators=(",", ":"), default=float).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    kb = (DOCS / "index.html").stat().st_size / 1024
    print(f"docs/index.html written ({kb:.0f} KB, {n_rel} releases)")


if __name__ == "__main__":
    main()

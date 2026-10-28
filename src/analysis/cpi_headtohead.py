"""Phase 4 (headline): Kalshi vs. the Cleveland Fed nowcast on monthly CPI.

Usage:  .venv/Scripts/python -m src.analysis.cpi_headtohead [--boot 1000]
Reads:  v_cpi_horizon_panel, v_nowcast_errors (Postgres)
Writes: outputs/phase4/*.csv, *.html; data/processed/cpi_probability_paths.csv (for the dashboard)

Unit of analysis: one CPI release x one horizon. Each source gets one score per release
(averaged over that release's strikes), so the 5-14 strikes of one release are never treated
as independent observations. Sample size = number of releases (53), stated everywhere.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import statsmodels.api as sm
from scipy import stats
from sklearn.isotonic import IsotonicRegression

from src.analysis import nowcast_probs as npb
from src.analysis import scoring
from src.analysis.db import OUTPUT_DIR, query
from src.ingest.http_cache import PROJECT_ROOT

OUT = OUTPUT_DIR / "phase4"
EVAL_START = pd.Timestamp("2022-03-01", tz="UTC")   # head-to-head sample starts here (DECISIONS.md)
PRIMARY_MODEL = "t0"                                 # chosen on pre-sample data, see choose_model
P_CLIP = (0.005, 0.995)
BLUE, ORANGE, AQUA, GRAY, INK, INK2 = "#2a78d6", "#eb6834", "#1baf7a", "#9a9993", "#0b0b0b", "#52514e"


# ---------------------------------------------------------------- probabilities

def kalshi_survival(strikes: np.ndarray, mids: np.ndarray) -> np.ndarray:
    """Repair Kalshi's strike prices into a valid survival curve P(CPI > k).

    Each strike is a separate binary market, so there's no "overround" to divide out; the only
    coherence condition is that P(CPI > k) can't rise as k rises. Isotonic (pool-adjacent-
    violators) regression finds the closest non-increasing curve in squared error."""
    order = np.argsort(strikes)
    iso = IsotonicRegression(increasing=False, y_min=0.0, y_max=1.0)
    fitted = np.empty_like(mids, dtype=float)
    fitted[order] = iso.fit_transform(strikes[order], mids[order])
    return fitted


def build_forecasts(model: str) -> pd.DataFrame:
    panel = query("SELECT * FROM v_cpi_horizon_panel WHERE strike_listed AND mid IS NOT NULL")
    errors = query("SELECT * FROM v_nowcast_errors")
    rows = []
    for (ev, h), g in panel.groupby(["event_ticker", "horizon_days"]):
        r0 = g.iloc[0]
        eff_days = (pd.Timestamp(r0.asof_ts) - pd.Timestamp(query_release(ev))).total_seconds() / -86400
        bucket = npb.horizon_bucket(eff_days)
        past = npb.history_before(errors, bucket, r0.asof_ts)
        if len(past) < npb.MIN_HISTORY or pd.isna(r0.nowcast_value):
            continue
        dist = npb.fit_error_dist(past, model)
        strikes = g.strike.to_numpy(float)
        p_fed = npb.strike_probabilities(float(r0.nowcast_value), strikes, dist)
        raw = g.mid.to_numpy(float)
        p_k = kalshi_survival(strikes, raw)
        for i, (_, r) in enumerate(g.iterrows()):
            rows.append({"event_ticker": ev, "reference_month": r.reference_month, "horizon_days": h,
                         "asof_ts": r.asof_ts, "error_horizon_bucket": bucket, "n_history": len(past),
                         "ticker": r.ticker, "strike": strikes[i], "y": int(r.outcome_yes),
                         "kalshi_raw_mid": raw[i], "p_kalshi": p_k[i], "p_fed": p_fed[i],
                         "nowcast": float(r0.nowcast_value), "err_scale": dist.scale, "err_df": dist.df,
                         "yes_bid": r.yes_bid, "yes_ask": r.yes_ask, "first_release_value": r.first_release_value})
    f = pd.DataFrame(rows)
    f["p_combo"] = (f.p_kalshi + f.p_fed) / 2
    return f


_release_cache: dict = {}


def query_release(event_ticker: str):
    if not _release_cache:
        ev = query("SELECT event_ticker, release_ts FROM v_cpi_events")
        _release_cache.update(dict(zip(ev.event_ticker, ev.release_ts)))
    return _release_cache[event_ticker]


# ---------------------------------------------------------------- scoring

SOURCES = {"kalshi": "p_kalshi", "fed": "p_fed", "combo": "p_combo"}


def per_release_scores(f: pd.DataFrame) -> pd.DataFrame:
    """One row per (release, horizon): each source's mean Brier, RPS and log score over strikes.

    With cumulative threshold contracts, RPS over the listed thresholds equals the SUM of the
    per-strike Brier scores, so it differs from mean Brier only in how releases are weighted
    (releases with more strikes count more)."""
    def agg(g):
        out = {"n_strikes": len(g)}
        for s, col in SOURCES.items():
            p = g[col].clip(*P_CLIP)
            out[f"brier_{s}"] = scoring.brier(p, g.y)
            out[f"rps_{s}"] = scoring.rps(1 - p, 1 - g.y)          # CDF form: P(CPI <= k)
            out[f"log_{s}"] = scoring.log_loss(p, g.y)
        return pd.Series(out)
    return f.groupby(["event_ticker", "reference_month", "horizon_days"]).apply(agg).reset_index()


def diebold_mariano(d: np.ndarray) -> dict:
    """DM test on a loss differential d_t (one per release, in time order).
    HAC (Newey-West) variance, Harvey-Leybourne-Newbold small-sample correction, t(T-1) reference.
    d < 0 means the first source has lower loss (is more accurate)."""
    d = np.asarray(d, float)
    T = len(d)
    lag = int(np.floor(T ** (1 / 3)))
    dc = d - d.mean()
    gamma = [np.sum(dc[k:] * dc[:T - k]) / T for k in range(lag + 1)]
    var = gamma[0] + 2 * sum((1 - k / (lag + 1)) * gamma[k] for k in range(1, lag + 1))
    dm = d.mean() / np.sqrt(var / T)
    hln = dm * np.sqrt((T - 1) / T)            # one-step (non-overlapping monthly releases)
    p = 2 * stats.t.sf(abs(hln), T - 1)
    return {"T": T, "mean_diff": d.mean(), "dm_stat": hln, "p_value": p, "nw_lag": lag,
            "share_first_better": float((d < 0).mean())}


def compare(scores: pd.DataFrame, metric: str, a: str, b: str, n_boot: int) -> pd.DataFrame:
    rows = []
    for h, g in scores.sort_values("reference_month").groupby("horizon_days"):
        d = (g[f"{metric}_{a}"] - g[f"{metric}_{b}"]).to_numpy()
        rng = np.random.default_rng(0)
        boots = [rng.choice(d, len(d)).mean() for _ in range(n_boot)]
        rows.append({"horizon_days": h, "metric": metric, "a": a, "b": b,
                     f"mean_{a}": g[f"{metric}_{a}"].mean(), f"mean_{b}": g[f"{metric}_{b}"].mean(),
                     "diff_ci_low": np.quantile(boots, .025), "diff_ci_high": np.quantile(boots, .975),
                     **diebold_mariano(d)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- encompassing

def encompassing(f: pd.DataFrame) -> pd.DataFrame:
    """logit P(y) = a + bK*logit(p_kalshi) + bF*logit(p_fed), SEs clustered by release.
    bF significantly > 0 => the Fed has information Kalshi's price lacks (and vice versa)."""
    rows = []
    for h, g in [("pooled", f)] + list(f.groupby("horizon_days")):
        X = sm.add_constant(pd.DataFrame({"logit_kalshi": scoring.logit(g.p_kalshi.clip(*P_CLIP)),
                                          "logit_fed": scoring.logit(g.p_fed.clip(*P_CLIP))}))
        groups = pd.factorize(g.event_ticker)[0]
        res = sm.GLM(g.y.to_numpy(), X, family=sm.families.Binomial()).fit(cov_type="cluster",
                                                                          cov_kwds={"groups": groups})
        rows.append({"horizon_days": h, "n_strikes": len(g), "n_releases": g.event_ticker.nunique(),
                     **{f"{k}_{v}": res.__getattribute__(a)[k] for k in X.columns
                        for v, a in (("coef", "params"), ("se", "bse"), ("p", "pvalues"))}})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- charts

def chart_brier_by_horizon(scores: pd.DataFrame, path, n_boot: int):
    fig = go.Figure()
    for s, color, label in (("kalshi", BLUE, "Kalshi (crowd)"), ("fed", ORANGE, "Cleveland Fed nowcast"),
                            ("combo", AQUA, "50/50 average")):
        stats_ = []
        for h, g in scores.groupby("horizon_days"):
            v = g[f"brier_{s}"].to_numpy()
            rng = np.random.default_rng(1)
            b = [rng.choice(v, len(v)).mean() for _ in range(n_boot)]
            stats_.append((h, v.mean(), np.quantile(b, .025), np.quantile(b, .975), len(v)))
        h, m, lo, hi, n = map(np.array, zip(*stats_))
        fig.add_trace(go.Scatter(
            x=h, y=m, mode="lines+markers", name=label, line=dict(color=color, width=2),
            marker=dict(size=9, color=color, line=dict(color="white", width=2)),
            error_y=dict(type="data", symmetric=False, array=hi - m, arrayminus=m - lo, color=color, thickness=1.5, width=0),
            customdata=np.c_[lo, hi, n],
            hovertemplate=f"{label}<br>%{{x}} days before release: Brier %{{y:.4f}}"
                          "<br>95% CI %{customdata[0]:.4f}–%{customdata[1]:.4f} · %{customdata[2]} releases<extra></extra>"))
    fig.update_layout(template="plotly_white", title=dict(text="Forecast error by horizon (lower is better)",
                                                          font=dict(color=INK, size=15), automargin=True),
                      font=dict(color=INK2, size=13), height=520, autosize=True, hovermode="closest",
                      xaxis=dict(title="Days before CPI release", type="log", tickvals=[1, 3, 7, 14, 30],
                                 autorange="reversed", gridcolor="#ecebe7"),
                      yaxis=dict(title="Mean Brier score per strike", gridcolor="#ecebe7", rangemode="tozero"),
                      legend=dict(orientation="h", y=-0.22, yanchor="top"), margin=dict(l=60, r=20, t=60, b=120))
    fig.write_html(path, include_plotlyjs="cdn", default_width="100%", config={"responsive": True})


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=1000)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    # Error-model choice on pre-sample months only (released before the evaluation starts)
    errors = query("SELECT * FROM v_nowcast_errors")
    sel = npb.choose_model(errors, EVAL_START)
    sel_table = sel.groupby(["horizon_days", "model"]).log_score.mean().unstack()
    sel_table.loc["all"] = sel.groupby("model").log_score.mean()
    sel_table.to_csv(OUT / "error_model_selection_presample.csv")

    f = build_forecasts(PRIMARY_MODEL)
    f.to_csv(PROJECT_ROOT / "data" / "processed" / "cpi_probability_paths.csv", index=False)
    scores = per_release_scores(f)
    scores.to_csv(OUT / "scores_per_release.csv", index=False)

    comps = pd.concat([compare(scores, m, a, b, args.boot) for m in ("brier", "rps", "log")
                       for a, b in (("kalshi", "fed"), ("combo", "kalshi"), ("combo", "fed"))])
    comps.to_csv(OUT / "diebold_mariano.csv", index=False)
    enc = encompassing(f)
    enc.to_csv(OUT / "encompassing.csv", index=False)

    # Sensitivity: Fed probabilities under a normal error model instead of Student-t
    f_norm = build_forecasts("normal0")
    sens = compare(per_release_scores(f_norm), "brier", "kalshi", "fed", args.boot)
    sens.to_csv(OUT / "sensitivity_normal_errors.csv", index=False)

    chart_brier_by_horizon(scores, OUT / "brier_by_horizon.html", args.boot)

    pd.set_option("display.width", 220, "display.max_columns", 30)
    print("SAMPLE releases per horizon:", scores.groupby("horizon_days").event_ticker.nunique().to_dict(),
          "| strikes per horizon:", f.groupby("horizon_days").size().to_dict())
    print("Error-model selection (pre-sample mean log predictive density, higher = better):")
    print(sel_table.round(3).to_string())
    print("Fed error scale at 1-day horizon, first vs last release:",
          f[f.horizon_days == 1].sort_values("reference_month").err_scale.iloc[[0, -1]].round(3).tolist(),
          "df:", f[f.horizon_days == 1].sort_values("reference_month").err_df.iloc[[0, -1]].round(1).tolist())
    cols = ["horizon_days", "metric", "a", "b", "T", "mean_diff", "diff_ci_low", "diff_ci_high", "dm_stat",
            "p_value", "share_first_better"]
    print("\nHEAD-TO-HEAD (diff = a - b; negative => a more accurate)")
    for m in ("brier", "rps", "log"):
        c = comps[comps.metric == m]
        print(f"-- {m}")
        print(c[cols].round(4).to_string(index=False))
    print("\nMean Brier by horizon:")
    print(scores.groupby("horizon_days")[["brier_kalshi", "brier_fed", "brier_combo"]].mean().round(4).to_string())
    print("\nENCOMPASSING  logit P(y) = a + bK logit(pK) + bF logit(pF), cluster by release")
    print(enc[["horizon_days", "n_releases", "n_strikes", "logit_kalshi_coef", "logit_kalshi_se", "logit_kalshi_p",
               "logit_fed_coef", "logit_fed_se", "logit_fed_p"]].round(4).to_string(index=False))
    print("\nSENSITIVITY (normal errors) Kalshi - Fed Brier:")
    print(sens[["horizon_days", "mean_kalshi", "mean_fed", "mean_diff", "dm_stat", "p_value"]].round(4).to_string(index=False))
    (OUT / "summary.json").write_text(json.dumps({
        "releases": int(scores.event_ticker.nunique()), "primary_error_model": PRIMARY_MODEL}, indent=1))


if __name__ == "__main__":
    main()

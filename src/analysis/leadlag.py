"""Phase 5: who moves first, Kalshi or the Cleveland Fed nowcast?

Usage:  .venv/Scripts/python -m src.analysis.leadlag [--boot 1000]
Reads:  v_cpi_daily_snapshots, data_release_markers (Postgres)
Writes: outputs/phase5/*.csv, *.html; data/processed/cpi_daily_paths.csv (dashboard)

Timing (business day t, all ET):
  F_t  nowcast published ~10:00 on day t
  K_t  Kalshi implied CPI at 09:30 on day t   (primary: strictly before F_t exists)
So dK_t = K_t - K_{t-1} covers (t-1 09:30, t 09:30], which CONTAINS the publication of F_{t-1}.
  Fed -> Kalshi:  coefficient on dF_{t-1} in the dK_t equation   (market reacts to the nowcast)
  Kalshi -> Fed:  coefficient on dK_{t-1} in the dF_t equation   (nowcast later follows the market;
                  the Fed model doesn't use market prices, so this means Kalshi got there first)
  Same day:       corr(dK_t, dF_t). Kalshi's window ends 30 minutes before F_t is published, so
                  this is shared information (e.g. yesterday's oil prices), not a reaction.
Changes are computed within a release only; SEs are clustered by release.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import statsmodels.formula.api as smf

from src.analysis.cpi_headtohead import kalshi_survival
from src.analysis.db import OUTPUT_DIR, query
from src.analysis.nowcast_probs import ROUNDING_OFFSET
from src.ingest.http_cache import PROJECT_ROOT

OUT = OUTPUT_DIR / "phase5"
MAX_GAP_DAYS = 4      # consecutive nowcast days may straddle a weekend/holiday, no more
LAGS = 2
BLUE, ORANGE, GRAY, INK, INK2 = "#2a78d6", "#eb6834", "#9a9993", "#0b0b0b", "#52514e"


# ---------------------------------------------------------------- implied CPI from strike prices

def implied_median(strikes, survival) -> float:
    """Median of Kalshi's implied distribution of UNROUNDED CPI (same scale as the nowcast).

    survival[i] = P(printed > strike_i) = P(unrounded >= strike_i + 0.05). Linear interpolation
    between the two thresholds that bracket 50%. NaN if 50% lies outside the listed strikes
    (no extrapolation into tails the market didn't price)."""
    k = np.asarray(strikes, float)
    s = np.asarray(survival, float)
    o = np.argsort(k)
    u, s = k[o] + ROUNDING_OFFSET, s[o]
    if s[0] < 0.5 or s[-1] > 0.5:
        return np.nan
    i = int(np.nonzero(s >= 0.5)[0].max())          # last threshold still at/above 50%
    if i == len(s) - 1 or s[i] == s[i + 1]:
        return float(u[i])
    return float(u[i] + (s[i] - 0.5) / (s[i] - s[i + 1]) * (u[i + 1] - u[i]))


def implied_mean(strikes, survival) -> float:
    """Expected PRINTED value with tails truncated at the listed strikes (robustness only):
    E = k_1 + sum_i S_i (k_{i+1} - k_i) + 0.1 * S_n. It jumps when strikes are added, so its
    changes are only used between days with the same strike set."""
    k = np.asarray(strikes, float)
    s = np.asarray(survival, float)
    o = np.argsort(k)
    k, s = k[o], s[o]
    return float(k[0] + np.sum(s[:-1] * np.diff(k)) + 0.1 * s[-1])


def daily_series(snapshot: str) -> pd.DataFrame:
    q = query("SELECT * FROM v_cpi_daily_snapshots WHERE snapshot = %(s)s", s=snapshot)
    rows = []
    for (ev, d), g in q.groupby(["event_ticker", "nowcast_date"]):
        strikes = g.strike.to_numpy(float)
        surv = kalshi_survival(strikes, g.mid.to_numpy(float))
        rows.append({"event_ticker": ev, "reference_month": g.reference_month.iloc[0], "date": pd.Timestamp(d),
                     "nowcast": float(g.nowcast_value.iloc[0]), "k_median": implied_median(strikes, surv),
                     "k_mean": implied_mean(strikes, surv), "strike_set": tuple(sorted(strikes)),
                     "n_strikes": len(strikes)})
    return pd.DataFrame(rows).sort_values(["event_ticker", "date"]).reset_index(drop=True)


def add_changes(df: pd.DataFrame, kcol: str) -> pd.DataFrame:
    d = df.copy()
    g = d.groupby("event_ticker")
    gap = g.date.diff().dt.days
    ok = gap.between(1, MAX_GAP_DAYS)
    d["dF"] = g.nowcast.diff().where(ok)
    d["dK"] = g[kcol].diff().where(ok)
    if kcol == "k_mean":   # only compare days with an identical strike set
        d.loc[g.strike_set.shift().ne(d.strike_set) | ~ok, "dK"] = np.nan
    for j in range(1, LAGS + 1):
        d[f"dF_l{j}"] = d.groupby("event_ticker").dF.shift(j)
        d[f"dK_l{j}"] = d.groupby("event_ticker").dK.shift(j)
    return d


# ---------------------------------------------------------------- tests

def granger(d: pd.DataFrame) -> dict:
    """Two panel regressions with release-clustered SEs, plus joint Wald tests on cross lags."""
    out = {}
    lagK = " + ".join(f"dK_l{j}" for j in range(1, LAGS + 1))
    lagF = " + ".join(f"dF_l{j}" for j in range(1, LAGS + 1))
    for name, formula, cross in (("K_eq", f"dK ~ {lagK} + {lagF}", [f"dF_l{j}" for j in range(1, LAGS + 1)]),
                                 ("F_eq", f"dF ~ {lagF} + {lagK}", [f"dK_l{j}" for j in range(1, LAGS + 1)])):
        m = d.dropna(subset=["dK", "dF"] + [f"d{x}_l{j}" for x in "KF" for j in range(1, LAGS + 1)])
        res = smf.ols(formula, data=m).fit(cov_type="cluster", cov_kwds={"groups": pd.factorize(m.event_ticker)[0]})
        w = res.wald_test(", ".join(f"{c} = 0" for c in cross), scalar=True)
        out[name] = {"n": int(res.nobs), "n_releases": m.event_ticker.nunique(),
                     "coefs": res.params.to_dict(), "se": res.bse.to_dict(), "p": res.pvalues.to_dict(),
                     "cross_lags_wald": float(w.statistic), "cross_lags_p": float(w.pvalue)}
    return out


def cross_correlations(d: pd.DataFrame, n_boot: int, max_lag: int = 3) -> pd.DataFrame:
    """corr(dF_t, dK_{t+L}) pooled within releases. L > 0: Kalshi moves AFTER the nowcast.
    L < 0: Kalshi moved BEFORE. CIs: bootstrap over releases."""
    def ccf(df):
        out = {}
        for L in range(-max_lag, max_lag + 1):
            k = df.groupby("event_ticker").dK.shift(-L)
            m = df.dF.notna() & k.notna()
            out[L] = np.corrcoef(df.dF[m], k[m])[0, 1] if m.sum() > 10 else np.nan
        return out
    point = ccf(d)
    events = d.event_ticker.unique()
    groups = {e: g for e, g in d.groupby("event_ticker")}
    rng = np.random.default_rng(0)
    boots = []
    for _ in range(n_boot):
        draw = rng.choice(events, len(events))
        # relabel duplicates so within-release shifts never cross between copies
        b = pd.concat([groups[e].assign(event_ticker=f"{e}#{i}") for i, e in enumerate(draw)], ignore_index=True)
        boots.append(ccf(b))
    bt = pd.DataFrame(boots)
    return pd.DataFrame({"lag": list(point), "corr": list(point.values()),
                         "ci_low": bt.quantile(.025).values, "ci_high": bt.quantile(.975).values})


def mark_release_days(df: pd.DataFrame) -> pd.DataFrame:
    rel = query("SELECT DISTINCT release_date FROM data_release_markers")
    days = set(pd.to_datetime(rel.release_date))
    return df.assign(release_day=df.date.isin(days))


# ---------------------------------------------------------------- charts

def chart_ccf(c: pd.DataFrame, path):
    fig = go.Figure()
    fig.add_hline(y=0, line=dict(color=GRAY, width=1))
    fig.add_trace(go.Bar(x=c.lag, y=c["corr"], marker=dict(color=BLUE, cornerradius=4), width=0.55, name="Correlation",
                         error_y=dict(type="data", symmetric=False, array=c.ci_high - c["corr"],
                                      arrayminus=c["corr"] - c.ci_low, color=INK2, thickness=1.5, width=0),
                         customdata=np.c_[c.ci_low, c.ci_high],
                         hovertemplate="Lag %{x} days: r = %{y:.3f}<br>95% CI %{customdata[0]:.3f} to %{customdata[1]:.3f}<extra></extra>"))
    fig.update_layout(template="plotly_white", showlegend=False, height=460, autosize=True,
                      title=dict(text="Who moves first? Correlation of daily changes", font=dict(color=INK, size=15),
                                 automargin=True),
                      font=dict(color=INK2, size=13),
                      xaxis=dict(title="Kalshi lag vs. Fed (days; + = after)", dtick=1),
                      yaxis=dict(title="Correlation (95% CI)", gridcolor="#ecebe7"),
                      margin=dict(l=60, r=20, t=60, b=70))
    fig.write_html(path, include_plotlyjs="cdn", default_width="100%", config={"responsive": True})


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=1000)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    pre = mark_release_days(daily_series("pre_nowcast"))
    post = mark_release_days(daily_series("after_close"))
    pre.drop(columns="strike_set").to_csv(PROJECT_ROOT / "data" / "processed" / "cpi_daily_paths.csv", index=False)

    variants = {
        "primary: 09:30 snapshot, implied median": add_changes(pre, "k_median"),
        "excluding CPI/PCE release days": add_changes(pre, "k_median").pipe(lambda d: d[~d.release_day]),
        "16:00 snapshot (after nowcast)": add_changes(post, "k_median"),
        "implied mean, same strike set": add_changes(pre, "k_mean"),
    }
    summary_rows, ccfs = [], []
    for name, d in variants.items():
        g = granger(d)
        c = cross_correlations(d, args.boot if name.startswith("primary") else max(200, args.boot // 5))
        ccfs.append(c.assign(variant=name))
        same_day = c.loc[c.lag == 0, "corr"].item()
        summary_rows.append({
            "variant": name, "n_days": g["K_eq"]["n"], "n_releases": g["K_eq"]["n_releases"],
            "fed_to_kalshi_coef (dF_l1 in dK eq)": g["K_eq"]["coefs"]["dF_l1"],
            "fed_to_kalshi_p": g["K_eq"]["p"]["dF_l1"], "fed_to_kalshi_joint_p": g["K_eq"]["cross_lags_p"],
            "kalshi_to_fed_coef (dK_l1 in dF eq)": g["F_eq"]["coefs"]["dK_l1"],
            "kalshi_to_fed_p": g["F_eq"]["p"]["dK_l1"], "kalshi_to_fed_joint_p": g["F_eq"]["cross_lags_p"],
            "kalshi_own_lag1": g["K_eq"]["coefs"]["dK_l1"], "fed_own_lag1": g["F_eq"]["coefs"]["dF_l1"],
            "same_day_corr": same_day})
    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUT / "granger_summary.csv", index=False)
    ccf_all = pd.concat(ccfs)
    ccf_all.to_csv(OUT / "cross_correlations.csv", index=False)
    chart_ccf(ccf_all[ccf_all.variant.str.startswith("primary")], OUT / "cross_correlation.html")

    prim = variants["primary: 09:30 snapshot, implied median"]
    desc = pd.DataFrame({
        "days_with_both_levels": [int(prim[["nowcast", "k_median"]].notna().all(axis=1).sum())],
        "days_with_both_changes": [int(prim[["dF", "dK"]].notna().all(axis=1).sum())],
        "share_days_nowcast_unchanged": [float((prim.dF.abs() < 1e-9).mean())],
        "share_days_kalshi_unchanged": [float((prim.dK.abs() < 1e-9).mean())],
        "sd_dF": [prim.dF.std()], "sd_dK": [prim.dK.std()],
        "mean_abs_gap_kalshi_minus_nowcast": [(prim.k_median - prim.nowcast).abs().mean()],
    })
    desc.to_csv(OUT / "descriptives.csv", index=False)

    pd.set_option("display.width", 220, "display.max_columns", 30)
    print("DESCRIPTIVES"); print(desc.round(4).T.to_string())
    print("\nGRANGER-STYLE PANEL REGRESSIONS (cluster by release)")
    print(summary.round(4).T.to_string())
    print("\nCROSS-CORRELATION corr(dF_t, dK_t+L), primary")
    print(ccf_all[ccf_all.variant.str.startswith("primary")][["lag", "corr", "ci_low", "ci_high"]].round(3).to_string(index=False))
    print("\nCROSS-CORRELATION, robustness variants (lags -1..1)")
    print(ccf_all[~ccf_all.variant.str.startswith("primary") & ccf_all.lag.between(-1, 1)]
          .pivot(index="variant", columns="lag", values="corr").round(3).to_string())


if __name__ == "__main__":
    main()

"""Phase 3: calibration of Kalshi prices across the exchange-wide sample.

Usage:  .venv/Scripts/python -m src.analysis.calibration [--boot 500]
Reads:  v_calibration_points (Postgres)
Writes: outputs/phase3/*.csv (tables), outputs/phase3/*.html (charts), prints a summary.

Sample (see DECISIONS.md): each market's quote 1, 7 and 30 days before close. Empty books
dropped; primary price = bid/ask midpoint when the spread is <= 10 cents. Markets within an
event are correlated, so every standard error / CI is clustered (or bootstrapped) by event.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import statsmodels.api as sm
import statsmodels.formula.api as smf

from src.analysis import scoring, weighting
from src.analysis.db import OUTPUT_DIR, query

OUT = OUTPUT_DIR / "phase3"
MAX_SPREAD = 0.10
MIN_EVENTS_PER_CATEGORY = 30
PRICE_CLIP = (0.005, 0.995)   # Kalshi ticks are 1 cent; a 0/1 midpoint can't occur after the empty-book filter

# Literature values for the one-parameter weighting functions (sources in DECISIONS.md).
LITERATURE = [
    ("Tversky & Kahneman (1992), gains", "tk", 0.61),
    ("Tversky & Kahneman (1992), losses", "tk", 0.69),
    ("Camerer & Ho (1994)", "tk", 0.56),
    ("Wu & Gonzalez (1996)", "tk", 0.71),
    ("Prelec (1998), suggested", "prelec", 0.65),
]

# Chart palette: validated reference categorical slots 1-3 + neutral reference ink.
BLUE, ORANGE, AQUA, GRAY, INK, INK2 = "#2a78d6", "#eb6834", "#1baf7a", "#9a9993", "#0b0b0b", "#52514e"


# ---------------------------------------------------------------- data

def load() -> pd.DataFrame:
    df = query("SELECT * FROM v_calibration_points")
    df["y"] = df.outcome_yes.astype(int)
    df["empty_book"] = (df.yes_bid == 0) & (df.yes_ask >= 1)
    cats = df.groupby("category").event_ticker.nunique()
    keep = set(cats[cats >= MIN_EVENTS_PER_CATEGORY].index)
    df["category_grp"] = np.where(df.category.isin(keep), df.category, "Other")
    df["log_volume"] = np.log1p(df.market_volume.astype(float))
    return df


def primary_sample(df: pd.DataFrame) -> pd.DataFrame:
    d = df[~df.empty_book & df.mid.notna() & (df.spread <= MAX_SPREAD)].copy()
    d["price"] = d.mid.astype(float).clip(*PRICE_CLIP)
    return d


def robustness_samples(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    allq = df[~df.empty_book & df.mid.notna()].copy()
    allq["price"] = allq.mid.astype(float).clip(*PRICE_CLIP)
    last = df[df.last_price.notna()].copy()
    last["price"] = last.last_price.astype(float).clip(*PRICE_CLIP)
    return {"primary (mid, spread<=10c)": primary_sample(df), "all quotes (mid, any spread)": allq,
            "last trade price": last}


# ---------------------------------------------------------------- scores

def score_table(d: pd.DataFrame, by: list[str], n_boot: int) -> pd.DataFrame:
    rows = []
    for key, g in d.groupby(by) if by else [((), d)]:
        key = key if isinstance(key, tuple) else (key,)
        boots = scoring.cluster_bootstrap(g, "event_ticker", lambda s: scoring.brier(s.price, s.y), n_boot=n_boot)
        base = g.y.mean()
        bs = scoring.brier(g.price, g.y)
        mur = scoring.murphy_decomposition(g.price.values, g.y.values)
        rows.append({**dict(zip(by, key)), "n_points": len(g), "n_events": g.event_ticker.nunique(),
                     "base_rate": base, "brier": bs, "brier_ci_low": np.quantile(boots, .025),
                     "brier_ci_high": np.quantile(boots, .975), "log_loss": scoring.log_loss(g.price, g.y),
                     "brier_skill_vs_base_rate": 1 - bs / (base * (1 - base)) if 0 < base < 1 else np.nan,
                     **mur})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- regressions

def calibration_regression(d: pd.DataFrame) -> dict:
    """logit P(yes) = a + b * logit(price), SEs clustered by event.
    Perfect calibration: a = 0, b = 1. b > 1: prices too close to 50% (longshots over-priced)."""
    X = sm.add_constant(pd.DataFrame({"logit_price": scoring.logit(d.price)}))
    groups = pd.factorize(d.event_ticker)[0]
    res = sm.GLM(d.y.values, X, family=sm.families.Binomial()).fit(cov_type="cluster", cov_kwds={"groups": groups})
    a, b = res.params["const"], res.params["logit_price"]
    se_a, se_b = res.bse["const"], res.bse["logit_price"]
    joint = res.wald_test("const = 0, logit_price = 1", scalar=True)
    return {"n": len(d), "n_events": d.event_ticker.nunique(), "intercept": a, "intercept_se": se_a,
            "slope": b, "slope_se": se_b, "z_slope_eq_1": (b - 1) / se_b,
            "joint_wald_chi2": float(joint.statistic), "joint_p": float(joint.pvalue)}


def mispricing_model(d: pd.DataFrame):
    """Does mispricing depend on liquidity, spread, time-to-close, category?
    The logit(price) x log-volume interaction asks whether calibration slope changes with liquidity."""
    m = d.assign(lp=scoring.logit(d.price), vol_z=(d.log_volume - d.log_volume.mean()) / d.log_volume.std(),
                 spread_c=d.spread.astype(float) * 100, horizon=d.horizon_days.astype(str))
    formula = "y ~ lp + lp:vol_z + vol_z + spread_c + C(horizon, Treatment('1')) + C(category_grp, Treatment('Sports'))"
    return smf.glm(formula, data=m, family=sm.families.Binomial()).fit(
        cov_type="cluster", cov_kwds={"groups": pd.factorize(m.event_ticker)[0]})


# ---------------------------------------------------------------- prospect theory

def fit_with_ci(d: pd.DataFrame, form: str, n_boot: int, seed: int = 0) -> dict:
    est, ll = weighting.fit(d.price.values, d.y.values, form)
    boots = scoring.cluster_bootstrap(d, "event_ticker", lambda s: weighting.fit(s.price.values, s.y.values, form)[0],
                                      n_boot=n_boot, seed=seed)
    ll_null = -weighting.neg_loglik(1.0, d.price.values, d.y.values, form)
    return {"form": form, "estimate": est, "ci_low": np.quantile(boots, .025), "ci_high": np.quantile(boots, .975),
            "boot_se": boots.std(ddof=1), "share_boot_below_1": float((boots < 1).mean()),
            "loglik": ll, "loglik_no_distortion": ll_null, "n": len(d), "n_events": d.event_ticker.nunique(),
            "_boots": boots}


def heterogeneity_test(fits: list[dict]) -> dict:
    """Wald test that all groups share one parameter. Groups are disjoint sets of events, so
    their bootstrap estimates are independent: W = sum((g_k - g_pooled)^2 / se_k^2) ~ chi2(K-1),
    with g_pooled the inverse-variance-weighted mean."""
    from scipy.stats import chi2
    est = np.array([f["estimate"] for f in fits])
    var = np.array([f["boot_se"] for f in fits]) ** 2
    pooled = np.sum(est / var) / np.sum(1 / var)
    w = float(np.sum((est - pooled) ** 2 / var))
    return {"wald_chi2": w, "df": len(fits) - 1, "p_value": float(chi2.sf(w, len(fits) - 1)), "pooled": pooled}


# ---------------------------------------------------------------- charts

def _layout(fig, title, xt, yt):
    fig.update_layout(template="plotly_white", title=dict(text=title, font=dict(color=INK, size=15), automargin=True),
                      font=dict(color=INK2, size=13), xaxis=dict(title=xt, range=[0, 1], gridcolor="#ecebe7"),
                      yaxis=dict(title=yt, range=[0, 1], gridcolor="#ecebe7"), height=620, autosize=True,
                      legend=dict(orientation="h", y=-0.28, yanchor="top"), hovermode="closest", margin=dict(l=60, r=20, t=60, b=150))
    return fig


def chart_reliability(rel: pd.DataFrame, path):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(color=GRAY, dash="dash", width=2),
                             name="Perfect calibration", hoverinfo="skip"))
    for h, color in zip((1, 7, 30), (BLUE, ORANGE, AQUA)):
        r = rel[rel.horizon_days == h]
        fig.add_trace(go.Scatter(
            x=r.mean_price, y=r.observed_freq, mode="lines+markers", name=f"{h} day{'s' if h > 1 else ''} before close",
            line=dict(color=color, width=2), marker=dict(size=9, color=color, line=dict(color="white", width=2)),
            error_y=dict(type="data", symmetric=False, array=r.ci_high - r.observed_freq,
                         arrayminus=r.observed_freq - r.ci_low, color=color, thickness=1.5, width=0),
            customdata=np.c_[r.n, r.n_events, r.ci_low, r.ci_high],
            hovertemplate="Price %{x:.2f} → happened %{y:.1%}<br>95% CI %{customdata[2]:.1%}–%{customdata[3]:.1%}"
                          "<br>%{customdata[0]} prices, %{customdata[1]} events<extra></extra>"))
    _layout(fig, "Price vs. how often it happened", "Market price (implied probability)",
            "Observed frequency of YES").write_html(path, include_plotlyjs="cdn", default_width="100%", config={"responsive": True})


def chart_weighting(rel_pooled: pd.DataFrame, fits: dict, path):
    p = np.linspace(0.001, 0.999, 400)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(color=GRAY, dash="dash", width=2),
                             name="No distortion (price = probability)", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=p, y=weighting.tk_w(p, 0.61), mode="lines", line=dict(color=ORANGE, width=2, dash="dot"),
                             name="Tversky & Kahneman (1992) lab estimate, γ = 0.61",
                             hovertemplate="True %{x:.2f} → weight %{y:.3f}<extra>TK 1992</extra>"))
    tk = fits["tk"]
    fig.add_trace(go.Scatter(x=p, y=weighting.tk_w(p, tk["estimate"]), mode="lines", line=dict(color=BLUE, width=2),
                             name=f"Fitted to Kalshi, γ = {tk['estimate']:.2f} [{tk['ci_low']:.2f}, {tk['ci_high']:.2f}]",
                             hovertemplate="True %{x:.2f} → price %{y:.3f}<extra>Kalshi fit</extra>"))
    r = rel_pooled
    fig.add_trace(go.Scatter(
        x=r.observed_freq, y=r.mean_price, mode="markers", name="Kalshi, binned (observed frequency, mean price)",
        marker=dict(size=9, color=BLUE, line=dict(color="white", width=2)),
        error_x=dict(type="data", symmetric=False, array=r.ci_high - r.observed_freq,
                     arrayminus=r.observed_freq - r.ci_low, color=BLUE, thickness=1.5, width=0),
        customdata=np.c_[r.n], hovertemplate="Happened %{x:.1%} · priced %{y:.2f}<br>%{customdata[0]} prices<extra></extra>"))
    _layout(fig, "True probability vs. Kalshi price", "True probability (observed frequency)",
            "Market price").write_html(path, include_plotlyjs="cdn", default_width="100%", config={"responsive": True})


def chart_groups(groups: pd.DataFrame, path):
    g = groups.sort_values("estimate")
    fig = go.Figure()
    fig.add_vline(x=1, line=dict(color=GRAY, dash="dash", width=2))
    fig.add_vline(x=0.61, line=dict(color=ORANGE, dash="dot", width=2))
    fig.add_trace(go.Scatter(
        x=g.estimate, y=g.label, mode="markers", marker=dict(size=10, color=BLUE, line=dict(color="white", width=2)),
        error_x=dict(type="data", symmetric=False, array=g.ci_high - g.estimate, arrayminus=g.estimate - g.ci_low,
                     color=BLUE, thickness=1.5, width=0), showlegend=False,
        customdata=np.c_[g.ci_low, g.ci_high, g.n_events],
        hovertemplate="%{y}: γ = %{x:.2f} [%{customdata[0]:.2f}, %{customdata[1]:.2f}]<br>%{customdata[2]} events<extra></extra>"))
    fig.update_layout(template="plotly_white", title=dict(text="Distortion γ by group "
                                                               "(1 = none; dotted = lab 0.61)",
                                                          font=dict(color=INK, size=15)),
                      font=dict(color=INK2, size=13), xaxis=dict(title="Fitted Tversky–Kahneman γ (95% CI)"),
                      height=40 * len(g) + 160, autosize=True, margin=dict(l=200, r=30, t=70, b=60))
    fig.write_html(path, include_plotlyjs="cdn", default_width="100%", config={"responsive": True})


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=500)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    raw = load()
    d = primary_sample(raw)
    summary = {"raw_points": len(raw), "primary_points": len(d), "primary_events": d.event_ticker.nunique(),
               "primary_markets": d.ticker.nunique(), "dropped_empty_book": int(raw.empty_book.sum()),
               "dropped_wide_spread": int((~raw.empty_book & (raw.spread > MAX_SPREAD)).sum())}

    # 1. Scores
    overall = score_table(d, [], args.boot)
    by_h = score_table(d, ["horizon_days"], args.boot)
    by_cat = score_table(d, ["category_grp"], args.boot)
    for name, t in (("scores_overall", overall), ("scores_by_horizon", by_h), ("scores_by_category", by_cat)):
        t.to_csv(OUT / f"{name}.csv", index=False)

    # 2. Reliability
    rel = pd.concat([scoring.reliability_table(g, n_boot=args.boot).assign(horizon_days=h)
                     for h, g in d.groupby("horizon_days")])
    rel_pooled = scoring.reliability_table(d, n_boot=args.boot)
    rel.to_csv(OUT / "reliability_by_horizon.csv", index=False)
    rel_pooled.to_csv(OUT / "reliability_pooled.csv", index=False)

    # 3. Favorite-longshot: calibration regression (primary + robustness samples, + per horizon)
    reg_rows = [{"sample": k, **calibration_regression(v)} for k, v in robustness_samples(raw).items()]
    reg_rows += [{"sample": f"primary, {h}d before close", **calibration_regression(g)} for h, g in d.groupby("horizon_days")]
    regs = pd.DataFrame(reg_rows)
    regs.to_csv(OUT / "calibration_regression.csv", index=False)

    # 4. Mispricing model
    mm = mispricing_model(d)
    coefs = pd.DataFrame({"coef": mm.params, "se_cluster": mm.bse, "z": mm.tvalues, "p": mm.pvalues})
    coefs.to_csv(OUT / "mispricing_model.csv")

    # 5. Prospect-theory weighting
    fits = {form: fit_with_ci(d, form, args.boot) for form in ("tk", "prelec")}
    fits_h = [{"horizon_days": h, **{k: v for k, v in fit_with_ci(g, "tk", args.boot).items() if k != "_boots"}}
              for h, g in d.groupby("horizon_days")]
    terc = pd.qcut(d.log_volume, 3, labels=["low", "mid", "high"])
    group_fits = []
    for kind, col in (("category", d.category_grp), ("liquidity tercile", terc)):
        for level, g in d.groupby(col, observed=True):
            f = fit_with_ci(g, "tk", args.boot, seed=1)
            group_fits.append({"kind": kind, "level": str(level), "label": f"{kind}: {level}", **f})
    het = {kind: heterogeneity_test([f for f in group_fits if f["kind"] == kind])
           for kind in ("category", "liquidity tercile")}
    lo = next(f for f in group_fits if f["label"] == "liquidity tercile: low")
    hi = next(f for f in group_fits if f["label"] == "liquidity tercile: high")
    diff = hi["_boots"] - lo["_boots"]
    het["high_minus_low_liquidity"] = {"estimate": hi["estimate"] - lo["estimate"], "ci_low": float(np.quantile(diff, .025)),
                                       "ci_high": float(np.quantile(diff, .975))}
    strip = lambda f: {k: v for k, v in f.items() if k != "_boots"}  # noqa: E731
    pd.DataFrame([strip(f) for f in fits.values()]).to_csv(OUT / "weighting_fit_pooled.csv", index=False)
    pd.DataFrame(fits_h).to_csv(OUT / "weighting_fit_by_horizon.csv", index=False)
    groups = pd.DataFrame([strip(f) for f in group_fits])
    groups.to_csv(OUT / "weighting_fit_by_group.csv", index=False)
    pd.DataFrame(LITERATURE, columns=["source", "form", "value"]).to_csv(OUT / "weighting_literature.csv", index=False)
    (OUT / "summary.json").write_text(json.dumps({"sample": summary, "heterogeneity": het}, indent=2, default=float))

    # 6. Charts
    chart_reliability(rel, OUT / "reliability.html")
    chart_weighting(rel_pooled, fits, OUT / "weighting_curve.html")
    chart_groups(groups, OUT / "weighting_by_group.html")

    # ---- printed summary (aggregates only)
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print("SAMPLE", summary)
    print("\nSCORES overall / by horizon")
    print(pd.concat([overall, by_h])[["horizon_days", "n_points", "n_events", "base_rate", "brier", "brier_ci_low",
                                      "brier_ci_high", "log_loss", "brier_skill_vs_base_rate", "reliability",
                                      "resolution"]].round(4).to_string(index=False))
    print("\nSCORES by category")
    print(by_cat[["category_grp", "n_points", "n_events", "brier", "brier_ci_low", "brier_ci_high",
                  "brier_skill_vs_base_rate", "reliability"]].round(4).to_string(index=False))
    print("\nRELIABILITY pooled")
    print(rel_pooled[["bin", "n", "n_events", "mean_price", "observed_freq", "ci_low", "ci_high"]].round(3).to_string(index=False))
    print("\nCALIBRATION REGRESSION  logit P(yes) = a + b*logit(price)")
    print(regs[["sample", "n", "n_events", "intercept", "intercept_se", "slope", "slope_se", "z_slope_eq_1",
                "joint_p"]].round(4).to_string(index=False))
    print("\nMISPRICING MODEL (cluster-robust by event)")
    print(coefs.round(4).to_string())
    print("\nWEIGHTING pooled")
    print(pd.DataFrame([strip(f) for f in fits.values()])[["form", "estimate", "ci_low", "ci_high",
                                                           "share_boot_below_1", "loglik", "loglik_no_distortion"]].round(4).to_string(index=False))
    print("\nWEIGHTING by horizon (TK)")
    print(pd.DataFrame(fits_h)[["horizon_days", "estimate", "ci_low", "ci_high", "n_events"]].round(3).to_string(index=False))
    print("\nWEIGHTING by group (TK)")
    print(groups[["label", "estimate", "ci_low", "ci_high", "n_events"]].round(3).to_string(index=False))
    print("\nHETEROGENEITY", json.dumps(het, indent=1, default=float))


if __name__ == "__main__":
    main()

"""Phase 6: does trading Kalshi CPI strikes on the Fed nowcast make money after costs?

Usage:  .venv/Scripts/python -m src.analysis.backtest [--boot 2000]
Reads:  the Phase 4 forecasts (rebuilt from Postgres via cpi_headtohead.build_forecasts)
Writes: outputs/phase6/*.csv, *.html

Rule, at each decision time (30/14/7/3/1 days before release) and each strike:
  p_fed - ask > theta  -> buy YES at the ask        pays $1 if CPI prints above the strike
  bid - p_fed > theta  -> buy NO at (1 - bid)       pays $1 if it doesn't
  (Buying NO at 1 - bid is economically the same as selling YES at the bid.)
Every position is held to settlement. Orders are 100 contracts, taker fees per fees.py.
theta is chosen on the TRAIN period only and then applied once to the TEST period.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from src.analysis.cpi_headtohead import PRIMARY_MODEL, build_forecasts
from src.analysis.db import OUTPUT_DIR
from src.analysis.fees import fee

OUT = OUTPUT_DIR / "phase6"
CONTRACTS = 100
TEST_START = pd.Timestamp("2025-01-01")
THETAS = np.round(np.arange(0.0, 0.305, 0.01), 2)
MIN_TRAIN_TRADES = 20
BLUE, ORANGE, AQUA, GRAY, INK, INK2 = "#2a78d6", "#eb6834", "#1baf7a", "#9a9993", "#0b0b0b", "#52514e"

COST_MODELS = {
    "mid, no fees": "signal and fill at the bid/ask midpoint, no fees (pure forecasting edge)",
    "bid/ask, no fees": "signal and fill at the actual bid/ask, no fees",
    "bid/ask + fees": "actual bid/ask plus Kalshi taker fees (realistic)",
}


def trades(f: pd.DataFrame, theta: float, cost: str) -> pd.DataFrame:
    """All trades triggered at threshold theta under a cost model. One row per trade, P&L per contract."""
    d = f.copy()
    bid, ask, p, y = d.yes_bid.astype(float), d.yes_ask.astype(float), d.p_fed, d.y
    if cost == "mid, no fees":
        mid = (bid + ask) / 2
        buy_yes, buy_no = p - mid > theta, mid - p > theta
        price_yes, price_no = mid, 1 - mid
    else:
        buy_yes = (p - ask > theta) & (ask < 1) & (ask > 0)
        buy_no = (bid - p > theta) & (bid > 0) & (bid < 1) & ~buy_yes
        price_yes, price_no = ask, 1 - bid
    rows = []
    for side, mask, price, payoff in (("YES", buy_yes, price_yes, y), ("NO", buy_no, price_no, 1 - y)):
        t = d[mask].assign(side=side, price=price[mask], payoff=payoff[mask])
        rows.append(t)
    t = pd.concat(rows, ignore_index=True)
    if t.empty:
        return t.assign(fee_per_contract=[], pnl=[])
    use_fees = cost == "bid/ask + fees"
    t["fee_per_contract"] = [fee(px, CONTRACTS) / CONTRACTS if use_fees else 0.0 for px in t.price]
    t["pnl"] = t.payoff - t.price - t.fee_per_contract          # dollars per contract
    return t


def summarize(t: pd.DataFrame, n_boot: int, seed: int = 0) -> dict:
    """P&L stats with a release-level bootstrap (strikes within a release share one outcome)."""
    if t.empty:
        return {"n_trades": 0, "n_releases": 0, "total_pnl_per_100": 0.0, "mean_pnl_cents": np.nan,
                "ci_low_cents": np.nan, "ci_high_cents": np.nan, "hit_rate": np.nan, "mean_price": np.nan,
                "mean_fee_cents": np.nan, "t_stat_release": np.nan}
    per_rel = t.groupby("event_ticker").pnl.agg(["sum", "count"])
    rng = np.random.default_rng(seed)
    idx = np.arange(len(per_rel))
    boots = []
    for _ in range(n_boot):
        s = per_rel.iloc[rng.choice(idx, len(idx))]
        boots.append(s["sum"].sum() / s["count"].sum())
    rel_sums = per_rel["sum"]
    t_stat = rel_sums.mean() / (rel_sums.std(ddof=1) / np.sqrt(len(rel_sums))) if len(rel_sums) > 1 else np.nan
    return {"n_trades": len(t), "n_releases": len(per_rel), "total_pnl_per_100": t.pnl.sum() * CONTRACTS,
            "mean_pnl_cents": t.pnl.mean() * 100, "ci_low_cents": np.quantile(boots, .025) * 100,
            "ci_high_cents": np.quantile(boots, .975) * 100, "hit_rate": float((t.payoff == 1).mean()),
            "mean_price": t.price.mean(), "mean_fee_cents": t.fee_per_contract.mean() * 100,
            "t_stat_release": t_stat}


def sweep(f: pd.DataFrame, n_boot: int) -> pd.DataFrame:
    rows = []
    for period, g in (("train", f[f.month < TEST_START]), ("test", f[f.month >= TEST_START])):
        for cost in COST_MODELS:
            for th in THETAS:
                rows.append({"period": period, "cost_model": cost, "theta": th,
                             **summarize(trades(g, th, cost), n_boot)})
    return pd.DataFrame(rows)


def choose_theta(sw: pd.DataFrame) -> tuple[float, bool]:
    """Threshold with the highest total net P&L on TRAIN (>= MIN_TRAIN_TRADES trades).
    Returns (theta, whether that best training P&L is positive)."""
    tr = sw[(sw.period == "train") & (sw.cost_model == "bid/ask + fees") & (sw.n_trades >= MIN_TRAIN_TRADES)]
    best = tr.sort_values(["total_pnl_per_100", "theta"], ascending=[False, True]).iloc[0]
    return float(best.theta), bool(best.total_pnl_per_100 > 0)


def chart_sweep(sw: pd.DataFrame, theta: float, path):
    fig = go.Figure()
    fig.add_hline(y=0, line=dict(color=GRAY, width=1))
    for period, color in (("train", BLUE), ("test", ORANGE)):
        s = sw[(sw.period == period) & (sw.cost_model == "bid/ask + fees") & (sw.n_trades > 0)]
        fig.add_trace(go.Scatter(
            x=s.theta, y=s.mean_pnl_cents, mode="lines+markers", name=f"{period.title()} period",
            line=dict(color=color, width=2), marker=dict(size=8, color=color, line=dict(color="white", width=2)),
            error_y=dict(type="data", symmetric=False, array=s.ci_high_cents - s.mean_pnl_cents,
                         arrayminus=s.mean_pnl_cents - s.ci_low_cents, color=color, thickness=1.2, width=0),
            customdata=np.c_[s.n_trades, s.n_releases, s.ci_low_cents, s.ci_high_cents],
            hovertemplate="θ = %{x:.2f}: %{y:.1f}¢ per contract<br>95% CI %{customdata[2]:.1f} to %{customdata[3]:.1f}¢"
                          "<br>%{customdata[0]} trades in %{customdata[1]} releases<extra></extra>"))
    fig.add_vline(x=theta, line=dict(color=INK2, dash="dot", width=1.5))
    fig.update_layout(template="plotly_white", height=500, autosize=True, hovermode="closest",
                      title=dict(text="Net profit per contract vs. signal threshold", font=dict(color=INK, size=15),
                                 automargin=True),
                      font=dict(color=INK2, size=13),
                      xaxis=dict(title="Threshold θ (Fed probability minus price)", gridcolor="#ecebe7"),
                      yaxis=dict(title="Mean P&L, cents per contract (after fees)", gridcolor="#ecebe7"),
                      legend=dict(orientation="h", y=-0.2, yanchor="top"), margin=dict(l=60, r=20, t=60, b=110))
    fig.write_html(path, include_plotlyjs="cdn", default_width="100%", config={"responsive": True})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=2000)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    f = build_forecasts(PRIMARY_MODEL)
    f["month"] = pd.to_datetime(f.reference_month).dt.tz_localize(None)
    split = {p: g.event_ticker.nunique() for p, g in (("train", f[f.month < TEST_START]), ("test", f[f.month >= TEST_START]))}

    sw = sweep(f, args.boot)
    sw.to_csv(OUT / "threshold_sweep.csv", index=False)
    theta, train_positive = choose_theta(sw)

    test = f[f.month >= TEST_START]
    final_rows, by_h = [], []
    for cost in COST_MODELS:
        t = trades(test, theta, cost)
        final_rows.append({"cost_model": cost, "theta": theta, **summarize(t, args.boot)})
    t_net = trades(test, theta, "bid/ask + fees")
    t_net.to_csv(OUT / "test_trades.csv", index=False)
    for (period, h), g in f.assign(period=np.where(f.month < TEST_START, "train", "test")).groupby(["period", "horizon_days"]):
        by_h.append({"period": period, "horizon_days": h, "theta": theta,
                     **summarize(trades(g, theta, "bid/ask + fees"), args.boot)})
    final = pd.DataFrame(final_rows)
    final.to_csv(OUT / "test_result.csv", index=False)
    pd.DataFrame(by_h).to_csv(OUT / "by_horizon.csv", index=False)
    chart_sweep(sw, theta, OUT / "threshold_sweep.html")

    pd.set_option("display.width", 220, "display.max_columns", 30)
    print(f"SPLIT releases: {split} (train < {TEST_START.date()} <= test)")
    print(f"CHOSEN theta on train: {theta:.2f}   best training net P&L positive? {train_positive}")
    cols = ["theta", "n_trades", "n_releases", "total_pnl_per_100", "mean_pnl_cents", "ci_low_cents",
            "ci_high_cents", "hit_rate", "mean_price", "mean_fee_cents", "t_stat_release"]
    for period in ("train", "test"):
        print(f"\nSWEEP {period}, net (bid/ask + fees), selected thetas")
        s = sw[(sw.period == period) & (sw.cost_model == "bid/ask + fees") & sw.theta.isin([0, .02, .05, .08, .1, .15, .2, .25, .3])]
        print(s[cols].round(3).to_string(index=False))
    print("\nCOST DECOMPOSITION, train, theta = 0.05 / 0.10")
    print(sw[(sw.period == "train") & sw.theta.isin([.05, .1])][["cost_model"] + cols].round(3).to_string(index=False))
    print(f"\nHELD-OUT TEST RESULT at theta = {theta:.2f}")
    print(final[["cost_model"] + cols].round(3).to_string(index=False))
    print("\nBY HORIZON at chosen theta (net)")
    print(pd.DataFrame(by_h)[["period", "horizon_days", "n_trades", "n_releases", "mean_pnl_cents", "ci_low_cents",
                              "ci_high_cents", "hit_rate"]].round(2).to_string(index=False))


if __name__ == "__main__":
    main()

"""Forecast scoring and calibration utilities, shared by every analysis phase.

All functions take probabilities p in [0, 1] and binary outcomes y in {0, 1}.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

EPS = 1e-6


def brier(p, y) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def log_loss(p, y) -> float:
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def rps(cdf_forecast, cdf_outcome) -> float:
    """Ranked probability score for ONE ordinal forecast.

    Both arguments are cumulative distributions over the same ordered categories, e.g.
    P(CPI <= k-th threshold). RPS = sum over thresholds of (F_k - O_k)^2 (unnormalized).
    """
    f, o = np.asarray(cdf_forecast, float), np.asarray(cdf_outcome, float)
    return float(np.sum((f - o) ** 2))


def murphy_decomposition(p, y, bins=np.linspace(0, 1, 11)) -> dict:
    """Brier = reliability - resolution + uncertainty (Murphy 1973), on binned forecasts.

    Reliability: how far binned forecasts sit from observed frequencies (lower is better).
    Resolution:  how much observed frequencies vary across bins (higher is better).
    The binned identity is approximate: within-bin forecast variance is not counted.
    """
    p, y = np.asarray(p, float), np.asarray(y, float)
    idx = np.clip(np.digitize(p, bins[1:-1]), 0, len(bins) - 2)
    base = y.mean()
    rel = res = 0.0
    for k in np.unique(idx):
        m = idx == k
        w = m.mean()
        rel += w * (p[m].mean() - y[m].mean()) ** 2
        res += w * (y[m].mean() - base) ** 2
    return {"reliability": rel, "resolution": res, "uncertainty": base * (1 - base)}


def cluster_bootstrap(df: pd.DataFrame, cluster: str, stat: Callable[[pd.DataFrame], float | np.ndarray],
                      n_boot: int = 1000, seed: int = 0) -> np.ndarray:
    """Resample whole clusters (e.g. events) with replacement; return the stat for each draw.

    Brackets/markets within one event are correlated (exactly one bracket wins), so
    resampling rows would understate uncertainty. Resampling events keeps that structure.
    """
    rng = np.random.default_rng(seed)
    df = df.reset_index(drop=True)
    row_lists = [idx for _, idx in sorted(df.groupby(cluster, sort=True).indices.items())]
    sizes = np.array([len(r) for r in row_lists])
    flat = np.concatenate(row_lists)
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    out = []
    for _ in range(n_boot):
        draw = rng.integers(0, len(row_lists), size=len(row_lists))
        rows = np.concatenate([flat[starts[k]:starts[k] + sizes[k]] for k in draw])
        out.append(stat(df.iloc[rows]))
    return np.asarray(out)


def block_bootstrap_indices(n: int, block: int, n_boot: int, seed: int = 0) -> np.ndarray:
    """Circular block bootstrap: each draw strings together blocks of `block` consecutive
    positions (wrapping around the end) until it has n positions. Shape (n_boot, n).

    Use it on series in TIME ORDER. Resampling observations one at a time assumes they're
    independent; blocks keep the short-run correlation that a HAC (Newey-West) test allows for,
    so the interval and the test agree."""
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return idx.reshape(n_boot, -1)[:, :n]


def block_bootstrap_mean_ci(x, block: int, n_boot: int = 2000, seed: int = 0, level: float = 0.95) -> tuple[float, float]:
    """Percentile CI for the mean of a time-ordered series, via the circular block bootstrap."""
    x = np.asarray(x, float)
    means = x[block_bootstrap_indices(len(x), block, n_boot, seed)].mean(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def reliability_table(df: pd.DataFrame, price: str = "price", outcome: str = "y", cluster: str = "event_ticker",
                      bins=(0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, 1.0), n_boot: int = 500,
                      seed: int = 0) -> pd.DataFrame:
    """Binned implied vs observed frequency, with event-clustered bootstrap 95% CIs."""
    d = df[[price, outcome, cluster]].copy()
    d["bin"] = pd.cut(d[price], bins=list(bins), include_lowest=True)
    rows = []
    for b, g in d.groupby("bin", observed=True):
        boots = cluster_bootstrap(g, cluster, lambda s: s[outcome].mean(), n_boot=n_boot, seed=seed)
        rows.append({"bin": str(b), "bin_low": b.left, "bin_high": b.right, "n": len(g),
                     "n_events": g[cluster].nunique(), "mean_price": g[price].mean(),
                     "observed_freq": g[outcome].mean(),
                     "ci_low": np.quantile(boots, 0.025), "ci_high": np.quantile(boots, 0.975)})
    return pd.DataFrame(rows)


def logit(p):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    return np.log(p / (1 - p))

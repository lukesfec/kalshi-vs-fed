"""Turn the Cleveland Fed's point nowcast into probabilities for Kalshi's CPI strikes.

Kalshi strike k pays if the PRINTED (one-decimal) CPI change is > k. Strikes sit on the
0.1 grid, so  printed > k  <=>  unrounded >= k + 0.05.  Model the unrounded first release as
    actual = nowcast + error,   error ~ D(theta_h)
so  P(YES at k) = P(error >= k + 0.05 - nowcast) = 1 - F_h(k + 0.05 - nowcast).

No lookahead: theta_h is estimated only from months whose CPI had been RELEASED before the
forecast's as-of time, at the same horizon h ("expanding window").

Error distributions compared (choice made on pre-sample data only, see choose_model):
  normal0   N(0, sigma)          unbiased nowcast
  normal    N(mu, sigma)         allows a persistent nowcast bias
  t0        Student-t(0, s, df)  fat tails
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

HORIZONS = (1, 3, 7, 14, 30)
ROUNDING_OFFSET = 0.05     # half of BLS's one-decimal rounding step
MIN_HISTORY = 24           # need >= 24 past errors before forecasting
MODELS = ("normal0", "normal", "t0")


@dataclass(frozen=True)
class ErrorDist:
    model: str
    loc: float
    scale: float
    df: float | None = None

    def sf(self, x):
        """P(error >= x)."""
        z = (np.asarray(x, float) - self.loc) / self.scale
        return stats.t.sf(z, self.df) if self.model == "t0" else stats.norm.sf(z)

    def logpdf(self, x):
        z = (np.asarray(x, float) - self.loc) / self.scale
        if self.model == "t0":
            return stats.t.logpdf(z, self.df) - np.log(self.scale)
        return stats.norm.logpdf(z) - np.log(self.scale)


def fit_error_dist(errors, model: str) -> ErrorDist:
    e = np.asarray(errors, float)
    if model == "normal0":
        return ErrorDist(model, 0.0, float(np.sqrt(np.mean(e ** 2))))
    if model == "normal":
        return ErrorDist(model, float(e.mean()), float(e.std(ddof=1)))
    if model == "t0":
        df, _, scale = stats.t.fit(e, floc=0.0)
        df = float(np.clip(df, 2.1, 200.0))   # keep variance finite; >200 is effectively normal
        _, _, scale = stats.t.fit(e, f0=df, floc=0.0)
        return ErrorDist(model, 0.0, float(scale), df)
    raise ValueError(model)


def horizon_bucket(effective_days: float) -> int:
    """Largest standard horizon <= the actual distance to release (>= 1).
    Normally equals the nominal horizon; differs only when the as-of time was capped at
    Kalshi's last trade (Sep 2025), where the forecast was made further from release."""
    eligible = [h for h in HORIZONS if h <= effective_days + 1e-9]
    return max(eligible) if eligible else HORIZONS[0]


def history_before(errors: pd.DataFrame, horizon: int, asof_ts) -> np.ndarray:
    """Errors at this horizon for months released strictly before asof_ts."""
    m = (errors.horizon_days == horizon) & (errors.release_ts < asof_ts)
    return errors.loc[m, "error"].to_numpy(float)


def strike_probabilities(nowcast: float, strikes, dist: ErrorDist) -> np.ndarray:
    return dist.sf(np.asarray(strikes, float) + ROUNDING_OFFSET - nowcast)


def choose_model(errors: pd.DataFrame, before_ts) -> pd.DataFrame:
    """Out-of-sample comparison on PRE-SAMPLE months only (released before `before_ts`):
    for each month with >= MIN_HISTORY prior errors, fit each model on the prior errors and
    score the realized error by log predictive density. Higher mean = better."""
    rows = []
    for h in HORIZONS:
        e = errors[errors.horizon_days == h].sort_values("release_ts")
        for _, r in e[e.release_ts < before_ts].iterrows():
            past = history_before(e, h, r.release_ts)
            if len(past) < MIN_HISTORY:
                continue
            for model in MODELS:
                rows.append({"horizon_days": h, "reference_month": r.reference_month, "model": model,
                             "log_score": float(fit_error_dist(past, model).logpdf(r.error))})
    return pd.DataFrame(rows)

"""Prospect-theory probability weighting functions, fit to prediction-market prices.

Model: the market price is a distorted version of the true probability,
    price = w(p_true)
so the probability implied for the outcome is p_true = w^{-1}(price). We choose the
distortion parameter that maximizes the Bernoulli likelihood of the realized outcomes.

  Tversky & Kahneman (1992):  w(p) = p^g / (p^g + (1-p)^g)^(1/g)
  Prelec (1998), one-param:   w(p) = exp(-(-ln p)^a)

Parameter < 1 -> inverse-S: small probabilities are over-priced and large ones
under-priced (the favorite-longshot pattern). Parameter = 1 -> no distortion.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize_scalar

EPS = 1e-6
BOUNDS = {"tk": (0.30, 3.0),     # TK's w is non-monotone below ~0.279
          "prelec": (0.05, 3.0)}


def tk_w(p, g):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    return p ** g / (p ** g + (1 - p) ** g) ** (1 / g)


def tk_inv(w, g, iters: int = 60):
    """Inverse of tk_w by vectorized bisection (tk_w is increasing on (0,1) for g >= 0.3)."""
    w = np.clip(np.asarray(w, float), EPS, 1 - EPS)
    lo, hi = np.zeros_like(w), np.ones_like(w)
    for _ in range(iters):
        mid = (lo + hi) / 2
        too_low = tk_w(mid, g) < w
        lo = np.where(too_low, mid, lo)
        hi = np.where(too_low, hi, mid)
    return (lo + hi) / 2


def prelec_w(p, a):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    return np.exp(-(-np.log(p)) ** a)


def prelec_inv(w, a):
    w = np.clip(np.asarray(w, float), EPS, 1 - EPS)
    return np.exp(-(-np.log(w)) ** (1 / a))


INVERSE = {"tk": tk_inv, "prelec": prelec_inv}
FORWARD = {"tk": tk_w, "prelec": prelec_w}


def _sufficient_stats(price, y):
    """Collapse to (unique price, #yes, #total). Exact for the Bernoulli likelihood, and fast:
    Kalshi midpoints sit on a half-cent grid, so there are only ~200 distinct prices."""
    u, inv = np.unique(np.asarray(price, float), return_inverse=True)
    y = np.asarray(y, float)
    return u, np.bincount(inv, weights=y, minlength=len(u)), np.bincount(inv, minlength=len(u)).astype(float)


def _nll_counts(param, u, k, n, form):
    q = np.clip(INVERSE[form](u, param), EPS, 1 - EPS)
    return float(-np.sum(k * np.log(q) + (n - k) * np.log(1 - q)))


def neg_loglik(param: float, price, y, form: str) -> float:
    return _nll_counts(param, *_sufficient_stats(price, y), form)


def fit(price, y, form: str) -> tuple[float, float]:
    """Maximum-likelihood distortion parameter. Returns (param, max log-likelihood)."""
    stats = _sufficient_stats(price, y)
    res = minimize_scalar(_nll_counts, bounds=BOUNDS[form], args=(*stats, form), method="bounded",
                          options={"xatol": 1e-5})
    return float(res.x), float(-res.fun)

"""Tests for the nowcast->probability conversion, Kalshi normalization, and the DM test."""
import numpy as np
import pandas as pd
import pytest

from src.analysis import nowcast_probs as npb
from src.analysis.cpi_headtohead import diebold_mariano, kalshi_survival
from src.load.load_db import cpi_strike


# ---------------------------------------------------------------- nowcast -> strike probabilities

def test_rounding_offset_matches_kalshi_rule():
    # Nowcast 0.24 with a vanishingly small error: printed value 0.2 -> YES above 0.1, NO above 0.2
    tiny = npb.ErrorDist("normal0", 0.0, 1e-6)
    p = npb.strike_probabilities(0.24, [0.1, 0.2], tiny)
    assert p[0] == pytest.approx(1) and p[1] == pytest.approx(0)
    # Nowcast exactly at the rounding boundary (0.25 prints as 0.3) -> coin flip for "above 0.2"
    assert npb.strike_probabilities(0.25, [0.2], tiny)[0] == pytest.approx(0.5)


def test_probabilities_decrease_with_strike():
    dist = npb.ErrorDist("t0", 0.0, 0.08, df=3.0)
    p = npb.strike_probabilities(0.2, np.arange(-0.3, 0.8, 0.1), dist)
    assert np.all(np.diff(p) < 0)


def test_normal_probability_value():
    # nowcast 0.20, sigma 0.10: P(unrounded >= 0.25) = P(Z >= 0.5) = 0.3085
    dist = npb.ErrorDist("normal0", 0.0, 0.10)
    assert npb.strike_probabilities(0.20, [0.2], dist)[0] == pytest.approx(0.30854, abs=1e-4)


def test_fit_normal0_is_rms():
    d = npb.fit_error_dist([0.1, -0.1, 0.2, -0.2], "normal0")
    assert d.loc == 0 and d.scale == pytest.approx(np.sqrt(0.025))


def test_fit_t_recovers_fat_tails():
    rng = np.random.default_rng(0)
    e = 0.1 * rng.standard_t(3, size=4000)
    d = npb.fit_error_dist(e, "t0")
    assert d.df == pytest.approx(3, abs=0.6) and d.scale == pytest.approx(0.1, abs=0.01)


def test_history_uses_only_months_released_before_asof():
    errors = pd.DataFrame({"horizon_days": [7, 7, 7, 1],
                           "release_ts": pd.to_datetime(["2024-01-11", "2024-02-13", "2024-03-12", "2024-01-11"], utc=True),
                           "error": [0.1, 0.2, 0.3, 9.9]})
    past = npb.history_before(errors, 7, pd.Timestamp("2024-02-13", tz="UTC"))   # same instant: not yet known
    assert past.tolist() == [0.1]


@pytest.mark.parametrize("days, bucket", [(1.0, 1), (3.0, 3), (7.0, 7), (9.0, 7), (30.0, 30), (0.4, 1), (45, 30)])
def test_horizon_bucket(days, bucket):
    assert npb.horizon_bucket(days) == bucket


# ---------------------------------------------------------------- Kalshi normalization

def test_kalshi_survival_already_monotone_is_unchanged():
    s = np.array([0.0, 0.1, 0.2, 0.3])
    p = np.array([0.95, 0.7, 0.3, 0.05])
    assert np.allclose(kalshi_survival(s, p), p)


def test_kalshi_survival_repairs_violation_by_pooling():
    s = np.array([0.0, 0.1, 0.2, 0.3])
    p = np.array([0.9, 0.4, 0.5, 0.1])          # 0.4 < 0.5 violates "P(CPI > k) non-increasing"
    out = kalshi_survival(s, p)
    assert np.all(np.diff(out) <= 1e-12)
    assert out[1] == pytest.approx(0.45) and out[2] == pytest.approx(0.45)   # PAV averages the pair


def test_kalshi_survival_handles_unsorted_input():
    s = np.array([0.2, 0.0, 0.1])
    p = np.array([0.3, 0.9, 0.6])
    assert np.allclose(kalshi_survival(s, p), p)


# ---------------------------------------------------------------- Diebold-Mariano

def test_dm_zero_mean_difference_is_insignificant():
    rng = np.random.default_rng(3)
    d = rng.normal(0, 1, 200)
    d -= d.mean()
    out = diebold_mariano(d)
    assert abs(out["dm_stat"]) < 1e-9 and out["p_value"] == pytest.approx(1.0)


@pytest.mark.parametrize("shift", [-0.35, -0.2, -0.12, 0.0, 0.15, 0.3])
def test_dm_interval_agrees_with_test(shift):
    rng = np.random.default_rng(5)
    d = rng.normal(shift, 1, 53)
    out = diebold_mariano(d)
    excludes_zero = out["diff_ci_high"] < 0 or out["diff_ci_low"] > 0
    assert excludes_zero == (out["p_value"] < 0.05)


def test_dm_detects_clear_difference_and_sign():
    rng = np.random.default_rng(4)
    out = diebold_mariano(rng.normal(-0.5, 1, 200))
    assert out["dm_stat"] < -3 and out["p_value"] < 0.01 and out["share_first_better"] > 0.5


# ---------------------------------------------------------------- strike parsing (pre-Dec-2022 markets)

@pytest.mark.parametrize("ticker, title, strike", [
    ("CPI-22AUG-TN0.4", "Will inflation rise more than -0.4% in August?", -0.4),
    ("CPI-22SEP-T0.2", "Will CPI rise more than 0.2% in September?", 0.2),
    ("CPI-22APR-T0.5", "Will inflation be above 0.5% in April?", 0.5),
])
def test_cpi_strike_from_ticker(ticker, title, strike):
    assert cpi_strike({"ticker": ticker, "title": title, "floor_strike": None}) == ("greater", strike)


def test_cpi_strike_prefers_api_field():
    assert cpi_strike({"ticker": "KXCPI-26JUL-T-0.4", "floor_strike": -0.4, "strike_type": "greater"}) == ("greater", -0.4)


def test_cpi_strike_title_mismatch_raises():
    with pytest.raises(ValueError):
        cpi_strike({"ticker": "CPI-22AUG-T0.3", "title": "Will inflation rise more than 0.4% in August?",
                    "floor_strike": None})

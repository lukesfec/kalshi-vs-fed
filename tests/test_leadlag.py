"""Tests for Kalshi implied-CPI summaries and lead-lag change alignment."""
import numpy as np
import pandas as pd
import pytest

from src.analysis.leadlag import add_changes, implied_mean, implied_median


def test_implied_median_interpolates_on_unrounded_scale():
    # P(printed > 0.1) = 0.8, P(printed > 0.2) = 0.2  -> 50% halfway between 0.15 and 0.25
    assert implied_median([0.1, 0.2], [0.8, 0.2]) == pytest.approx(0.20)


def test_implied_median_exact_threshold():
    assert implied_median([0.0, 0.1, 0.2], [0.9, 0.5, 0.1]) == pytest.approx(0.15)


def test_implied_median_nan_outside_listed_strikes():
    assert np.isnan(implied_median([0.1, 0.2], [0.4, 0.1]))   # median below the lowest strike
    assert np.isnan(implied_median([0.1, 0.2], [0.9, 0.7]))   # median above the highest strike


def test_implied_median_unsorted_input():
    assert implied_median([0.2, 0.1], [0.2, 0.8]) == pytest.approx(0.20)


def test_implied_mean_degenerate_distribution():
    # Certain the print is exactly 0.2: P(>0.1)=1, P(>0.2)=0
    assert implied_mean([0.0, 0.1, 0.2], [1.0, 1.0, 0.0]) == pytest.approx(0.2)


def _frame(dates, nowcast, k, event="E"):
    return pd.DataFrame({"event_ticker": event, "date": pd.to_datetime(dates), "nowcast": nowcast,
                         "k_median": k, "k_mean": k, "strike_set": [(0.1, 0.2)] * len(dates)})


def test_changes_skip_gaps_longer_than_weekend():
    d = add_changes(_frame(["2026-07-02", "2026-07-06", "2026-07-20"], [0.1, 0.2, 0.4], [0.1, 0.3, 0.6]), "k_median")
    assert d.dF.iloc[1] == pytest.approx(0.1)      # Thu -> Mon (4 days) is consecutive
    assert np.isnan(d.dF.iloc[2])                   # two-week hole is not a daily change


def test_changes_and_lags_never_cross_releases():
    a = _frame(["2026-07-01", "2026-07-02"], [0.1, 0.2], [0.1, 0.2], event="A")
    b = _frame(["2026-07-03", "2026-07-06"], [0.5, 0.7], [0.5, 0.9], event="B")
    d = add_changes(pd.concat([a, b]).sort_values(["event_ticker", "date"]).reset_index(drop=True), "k_median")
    first_b = d[d.event_ticker == "B"].iloc[0]
    assert np.isnan(first_b.dF) and np.isnan(first_b.dK_l1)


def test_implied_mean_changes_require_same_strike_set():
    f = _frame(["2026-07-01", "2026-07-02"], [0.1, 0.1], [0.1, 0.3])
    f.loc[1, "strike_set"] = (0.1, 0.2, 0.3)
    d = add_changes(f, "k_mean")
    assert np.isnan(d.dK.iloc[1])

"""Tests for scoring and probability-weighting functions."""
import numpy as np
import pandas as pd
import pytest

from src.analysis import scoring, weighting


def test_brier_and_log_loss_known_values():
    assert scoring.brier([0.8, 0.3], [1, 0]) == pytest.approx((0.04 + 0.09) / 2)
    assert scoring.log_loss([0.5, 0.5], [1, 0]) == pytest.approx(np.log(2))
    assert scoring.brier([1, 0], [1, 0]) == 0


def test_rps_perfect_and_worst():
    assert scoring.rps([0, 1, 1], [0, 1, 1]) == 0
    assert scoring.rps([1, 1, 1], [0, 0, 1]) == 2


def test_murphy_decomposition_identity_for_binned_forecasts():
    rng = np.random.default_rng(1)
    p = rng.choice([0.05, 0.25, 0.45, 0.65, 0.85], size=5000)   # one value per bin -> identity is exact
    y = (rng.random(5000) < p).astype(int)
    d = scoring.murphy_decomposition(p, y)
    assert scoring.brier(p, y) == pytest.approx(d["reliability"] - d["resolution"] + d["uncertainty"], abs=1e-12)


def test_cluster_bootstrap_resamples_whole_clusters():
    df = pd.DataFrame({"event": ["a", "a", "b", "b"], "y": [1, 1, 0, 0]})
    draws = scoring.cluster_bootstrap(df, "event", lambda s: s.y.mean(), n_boot=200, seed=0)
    assert set(np.round(draws, 2)) <= {0.0, 0.5, 1.0}   # only possible if clusters stay intact


@pytest.mark.parametrize("form", ["tk", "prelec"])
def test_weighting_inverse_roundtrip(form):
    p = np.linspace(0.01, 0.99, 50)
    for param in (0.5, 0.7, 1.0, 1.5):
        w = weighting.FORWARD[form](p, param)
        assert np.allclose(weighting.INVERSE[form](w, param), p, atol=1e-6)


@pytest.mark.parametrize("form", ["tk", "prelec"])
def test_parameter_one_is_identity(form):
    p = np.linspace(0.01, 0.99, 20)
    assert np.allclose(weighting.FORWARD[form](p, 1.0), p)


@pytest.mark.parametrize("form, true_param", [("tk", 0.65), ("prelec", 0.75), ("tk", 1.0)])
def test_fit_recovers_parameter_from_simulated_market(form, true_param):
    rng = np.random.default_rng(42)
    p_true = rng.uniform(0.02, 0.98, 40000)
    price = weighting.FORWARD[form](p_true, true_param)    # market distorts the true probability
    y = (rng.random(p_true.size) < p_true).astype(int)
    est, _ = weighting.fit(price, y, form)
    assert est == pytest.approx(true_param, abs=0.05)


def test_tk_literature_shape():
    # gamma = 0.61 (Tversky & Kahneman 1992, gains): small p overweighted, large p underweighted
    assert weighting.tk_w(0.05, 0.61) > 0.05
    assert weighting.tk_w(0.90, 0.61) < 0.90

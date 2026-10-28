"""Circular block bootstrap: shape, block structure, and wider intervals under autocorrelation."""
import numpy as np

from src.analysis.scoring import block_bootstrap_indices, block_bootstrap_mean_ci


def test_indices_shape_and_range():
    idx = block_bootstrap_indices(53, 3, 100, seed=0)
    assert idx.shape == (100, 53) and idx.min() >= 0 and idx.max() < 53


def test_indices_come_in_consecutive_circular_blocks():
    idx = block_bootstrap_indices(10, 3, 50, seed=1)
    for row in idx:
        for start in range(0, 9, 3):          # full blocks inside the first 9 positions
            b = row[start:start + 3]
            assert all((b[i + 1] - b[i]) % 10 == 1 for i in range(len(b) - 1))


def test_block_one_matches_iid_resampling_width():
    x = np.random.default_rng(0).normal(size=200)
    lo, hi = block_bootstrap_mean_ci(x, block=1, n_boot=4000)
    se = x.std(ddof=1) / np.sqrt(len(x))
    assert hi - lo == np.float64(hi - lo) and abs((hi - lo) - 2 * 1.96 * se) < 0.25 * 2 * 1.96 * se


def test_blocks_widen_interval_for_positively_autocorrelated_series():
    rng = np.random.default_rng(2)
    e = rng.normal(size=600)
    x = np.convolve(e, np.ones(4) / 4, mode="valid")        # MA(3): strong positive autocorrelation
    lo1, hi1 = block_bootstrap_mean_ci(x, block=1, n_boot=3000)
    lo4, hi4 = block_bootstrap_mean_ci(x, block=4, n_boot=3000)
    assert (hi4 - lo4) > 1.3 * (hi1 - lo1)

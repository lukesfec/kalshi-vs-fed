import pytest

from src.analysis.fees import MAKER_RATE, fee


def test_published_example_100_contracts_at_50c():
    assert fee(0.50, 100) == pytest.approx(1.75)


def test_round_up_to_cent():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> $0.02
    assert fee(0.50, 1) == pytest.approx(0.02)
    # 0.07 * 100 * 0.05 * 0.95 = 0.3325 -> $0.34
    assert fee(0.05, 100) == pytest.approx(0.34)


def test_symmetric_in_price():
    assert fee(0.30, 100) == fee(0.70, 100)


def test_maker_is_quarter_of_taker_before_rounding():
    assert fee(0.50, 10000, rate=MAKER_RATE) == pytest.approx(43.75)


def test_no_fee_at_certainty():
    assert fee(1.0, 100) == 0 and fee(0.0, 100) == 0

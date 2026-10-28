"""Backtest mechanics on hand-made quotes: signal sides, fill prices, payoffs, fees."""
import pandas as pd
import pytest

from src.analysis.backtest import choose_theta, trades
from src.analysis.fees import fee


def quotes(**kw):
    base = {"event_ticker": "E1", "yes_bid": 0.40, "yes_ask": 0.44, "p_fed": 0.42, "y": 1}
    return pd.DataFrame([{**base, **kw}])


def test_buy_yes_at_ask_when_fed_above_ask():
    t = trades(quotes(p_fed=0.60, y=1), 0.10, "bid/ask, no fees")
    assert len(t) == 1 and t.side.item() == "YES"
    assert t.price.item() == pytest.approx(0.44) and t.pnl.item() == pytest.approx(0.56)


def test_buy_no_at_one_minus_bid_when_fed_below_bid():
    t = trades(quotes(p_fed=0.20, y=1), 0.10, "bid/ask, no fees")
    assert t.side.item() == "NO" and t.price.item() == pytest.approx(0.60)
    assert t.pnl.item() == pytest.approx(-0.60)          # CPI printed above the strike: NO loses


def test_no_trade_inside_threshold():
    assert trades(quotes(p_fed=0.50), 0.10, "bid/ask, no fees").empty   # 0.50 - 0.44 = 0.06 < 0.10


def test_spread_blocks_trade_that_mid_would_take():
    q = quotes(p_fed=0.50)                               # mid 0.42: edge 0.08 vs mid, only 0.06 vs ask
    assert len(trades(q, 0.07, "mid, no fees")) == 1
    assert trades(q, 0.07, "bid/ask, no fees").empty


def test_fees_reduce_pnl_by_kalshi_formula():
    t = trades(quotes(p_fed=0.60, y=1), 0.10, "bid/ask + fees")
    assert t.fee_per_contract.item() == pytest.approx(fee(0.44, 100) / 100)
    assert t.pnl.item() == pytest.approx(0.56 - fee(0.44, 100) / 100)


def test_no_fill_against_empty_side():
    assert trades(quotes(p_fed=0.90, yes_ask=1.0), 0.05, "bid/ask, no fees").empty
    assert trades(quotes(p_fed=0.01, yes_bid=0.0), 0.05, "bid/ask, no fees").empty


def test_choose_theta_uses_train_only_and_min_trades():
    sw = pd.DataFrame([
        {"period": "train", "cost_model": "bid/ask + fees", "theta": 0.05, "n_trades": 50, "total_pnl_per_100": -10},
        {"period": "train", "cost_model": "bid/ask + fees", "theta": 0.10, "n_trades": 30, "total_pnl_per_100": 5},
        {"period": "train", "cost_model": "bid/ask + fees", "theta": 0.20, "n_trades": 5, "total_pnl_per_100": 99},
        {"period": "test", "cost_model": "bid/ask + fees", "theta": 0.05, "n_trades": 50, "total_pnl_per_100": 999},
    ])
    assert choose_theta(sw) == (0.10, True)

"""Kalshi trading fees.

Published schedule (see FEASIBILITY.md §4; KXCPI series: fee_type quadratic_with_maker_fees,
multiplier 1, confirmed via the API):
  taker fee = round_up_to_cent( 0.07   * M * C * P * (1 - P) )
  maker fee = round_up_to_cent( 0.0175 * M * C * P * (1 - P) )   (only when a resting order fills)
P = price in dollars of the side you buy, C = contracts, M = series multiplier.
P(1-P) is symmetric, so buying NO at (1 - bid) costs the same fee as selling YES at the bid.
"""
from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def fee(price: float, contracts: int, rate: float = TAKER_RATE, multiplier: float = 1.0) -> float:
    """Total fee in dollars for one order, rounded UP to the next cent (as Kalshi does)."""
    raw = rate * multiplier * contracts * price * (1 - price)
    return math.ceil(round(raw * 100, 9)) / 100   # round(...) guards against 1.7500000001 -> 1.76

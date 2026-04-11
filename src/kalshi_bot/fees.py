"""Kalshi general taker/maker fee formulas (see CFTC rulebook / kalshi.com fee schedule).

Fees = round up to next cent of the fee-rate × C × P × (1−P) with P in dollars on (0,1).
This peaks at P=0.50 (maximum uncertainty). Not legal advice; verify current schedule.
"""

from __future__ import annotations

import math


def _clamp_price_dollars(p: float) -> float:
    return max(0.01, min(0.99, p))


def kalshi_general_taker_fee_usd(*, contracts: int, price_dollars: float) -> float:
    """Taker fee in USD for the standard 7% coefficient schedule."""
    c = max(1, int(contracts))
    p = _clamp_price_dollars(price_dollars)
    raw = 0.07 * c * p * (1.0 - p)
    return math.ceil(raw * 100.0) / 100.0


def kalshi_general_maker_fee_usd(*, contracts: int, price_dollars: float) -> float:
    """Maker fee in USD (1.75% coefficient in the same P×(1−P) form)."""
    c = max(1, int(contracts))
    p = _clamp_price_dollars(price_dollars)
    raw = 0.0175 * c * p * (1.0 - p)
    return math.ceil(raw * 100.0) / 100.0


def taker_fee_per_contract_usd(price_dollars: float) -> float:
    """Average taker fee in USD for a 1-contract trade at ``price_dollars``."""
    return kalshi_general_taker_fee_usd(contracts=1, price_dollars=price_dollars)


def effective_fee_rate_taker(price_dollars: float) -> float:
    """Fee / price for a taker buy at ``price_dollars`` (rough intensity vs mid)."""
    p = _clamp_price_dollars(price_dollars)
    if p <= 0:
        return 0.0
    return taker_fee_per_contract_usd(p) / p

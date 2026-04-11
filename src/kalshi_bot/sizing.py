"""Scale max contracts and exposure caps from account balance (percentage of balance)."""

from __future__ import annotations

import math

from kalshi_bot.config import Settings


def effective_max_contracts(
    settings: Settings,
    *,
    balance_cents: int | None,
    yes_price_cents: int,
) -> int:
    """Cap order size: min(config max, balance × TRADE_RISK_PCT_OF_BALANCE_PER_TRADE / price)."""
    base = settings.max_contracts_per_market
    if not settings.trade_balance_sizing_enabled or balance_cents is None or balance_cents <= 0:
        return base
    price = max(1, min(99, yes_price_cents))
    budget = float(balance_cents) * settings.trade_risk_pct_of_balance_per_trade
    cap = int(budget // float(price))
    return max(1, min(base, cap))


def cap_buy_yes_count_for_notional(
    count: int,
    *,
    yes_price_cents: int,
    max_notional_usd: float | None,
    side: str,
    action: str,
) -> int:
    """Cap contracts for buy YES so approximate cash at limit stays ≤ ``max_notional_usd``.

    Uses limit price in dollars as ``yes_price_cents/100`` (same notion as exposure in ``execute_intent``).
    """
    if side != "yes" or action != "buy":
        return count
    if max_notional_usd is None or max_notional_usd <= 0:
        return count
    p = max(1, min(99, yes_price_cents)) / 100.0
    max_n = int(max_notional_usd / p)
    return max(0, min(count, max_n))


def adjust_buy_yes_count_for_notional_floor(
    count: int,
    *,
    yes_price_cents: int,
    min_notional_usd: float | None,
    max_notional_usd: float | None,
    max_contracts: int,
) -> int:
    """Raise buy-YES count to meet minimum $ at limit without exceeding max $ and max_contracts.

    Returns 0 if the minimum notional cannot be reached (e.g. cap too tight vs min).
    """
    if min_notional_usd is None or min_notional_usd <= 0:
        return max(0, count)
    p = max(1, min(99, yes_price_cents)) / 100.0
    max_n = max(0, max_contracts)
    if max_notional_usd is not None and max_notional_usd > 0:
        max_n = min(max_n, int(max_notional_usd / p))
    count = min(max(0, count), max_n)
    need = int(math.ceil(min_notional_usd / p))
    if need > max_n:
        return 0
    return max(count, need)


def effective_max_exposure_cents(settings: Settings, balance_cents: int | None) -> float:
    """Cap total exposure: min(MAX_EXPOSURE_CENTS, balance × TRADE_TOTAL_RISK_PCT_OF_BALANCE)."""
    static = float(settings.max_exposure_cents)
    if not settings.trade_balance_sizing_enabled or balance_cents is None or balance_cents <= 0:
        return static
    scaled = float(balance_cents) * settings.trade_total_risk_pct_of_balance
    return min(static, scaled)
